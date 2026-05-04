(function () {
  const ACCEPT = new Set(["image/jpeg", "image/png", "image/webp"]);
  const POLL_TIMEOUT_MS = 60_000;
  const POLL_INTERVAL_MS = 1500;
  const PUT_CONCURRENCY = 8;
  const PUT_RETRIES = 1;
  const PUT_RETRY_BASE_MS = 1000;
  const ADD_TO_ALBUM_CHUNK = 100;

  function pluralize(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  async function runPool(items, concurrency, worker) {
    let next = 0;
    const n = Math.min(concurrency, items.length);
    const workers = [];
    for (let w = 0; w < n; w++) {
      workers.push((async () => {
        while (true) {
          const i = next++;
          if (i >= items.length) return;
          await worker(items[i], i);
        }
      })());
    }
    await Promise.all(workers);
  }

  async function withRetry(fn, retries, baseMs) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await fn();
      } catch (err) {
        if (attempt >= retries) throw err;
        await new Promise(r => setTimeout(r, baseMs * (attempt + 1)));
      }
    }
  }

  async function presign(files) {
    const resp = await fetch("/api/uploads/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files: files.map(f => ({ filename: f.name, content_type: f.type })),
      }),
    });
    if (!resp.ok) throw new Error(`presign HTTP ${resp.status}`);
    return (await resp.json()).uploads;
  }

  async function putOne(file, info) {
    const r = await fetch(info.url, {
      method: "PUT",
      body: file,
      headers: { "Content-Type": file.type },
    });
    if (!r.ok) throw new Error(`PUT ${file.name}: HTTP ${r.status}`);
  }

  async function waitForProcessing(ids, onTick) {
    const start = Date.now();
    const want = new Set(ids);
    const found = new Set();
    while (found.size < want.size && Date.now() - start < POLL_TIMEOUT_MS) {
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
      try {
        const remaining = [...want].filter(id => !found.has(id));
        const resp = await fetch("/api/photos/exists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ photo_ids: remaining }),
        });
        if (!resp.ok) continue;
        const data = await resp.json();
        for (const pid of data.exists || []) found.add(pid);
        if (onTick) onTick(found.size);
      } catch (_e) {}
    }
    return Array.from(found);
  }

  async function addToAlbum(albumId, photoIds, onTick) {
    let totalAdded = 0;
    let title;
    for (let i = 0; i < photoIds.length; i += ADD_TO_ALBUM_CHUNK) {
      const chunk = photoIds.slice(i, i + ADD_TO_ALBUM_CHUNK);
      const resp = await fetch(
        `/api/albums/${encodeURIComponent(albumId)}/photos`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ photo_ids: chunk }),
        },
      );
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const data = await resp.json();
          if (data.detail) detail = data.detail;
        } catch (_e) {}
        throw new Error(detail);
      }
      const data = await resp.json();
      totalAdded += data.added;
      title = data.title;
      if (onTick) onTick(i + chunk.length);
    }
    return { added: totalAdded, title, album_id: albumId };
  }

  async function uploadFiles(files, opts = {}) {
    const { albumId, onStatus, onProgress } = opts;
    const setStatus = (t) => { if (onStatus) onStatus(t || ""); };
    const emit = (phase, done, total, failed = 0) => {
      if (onProgress) onProgress({ phase, done, total, failed });
    };

    const accepted = files.filter(f => ACCEPT.has(f.type));
    const rejected = files.length - accepted.length;
    if (!accepted.length) {
      setStatus(rejected ? `${rejected} unsupported file(s) skipped.` : "");
      return { processed: [], added: 0, errors: [] };
    }

    setStatus(`Requesting upload URLs for ${accepted.length} photo(s)…`);
    let uploads;
    try {
      uploads = await presign(accepted);
    } catch (err) {
      setStatus(`Failed to get upload URLs: ${err.message}`);
      return { processed: [], added: 0, errors: [err] };
    }

    const total = accepted.length;
    let succeeded = 0;
    const expectedIds = [];
    const errors = [];
    setStatus(`Uploading 0/${total}…`);
    emit("uploading", 0, total, 0);

    await runPool(accepted, PUT_CONCURRENCY, async (f, i) => {
      try {
        await withRetry(
          () => putOne(f, uploads[i]),
          PUT_RETRIES,
          PUT_RETRY_BASE_MS,
        );
        expectedIds.push(uploads[i].photo_id);
        succeeded++;
        setStatus(`Uploading ${succeeded}/${total}…`);
      } catch (e) {
        errors.push(e);
      }
      emit("uploading", succeeded + errors.length, total, errors.length);
    });

    if (!expectedIds.length) {
      setStatus(`Uploads failed: ${errors.length} error(s).`);
      return { processed: [], added: 0, errors };
    }

    if (errors.length) {
      setStatus(`Uploaded ${succeeded}/${total}. ${errors.length} failed. Processing…`);
    } else {
      setStatus(`Uploaded ${total}. Processing…`);
    }
    emit("processing", 0, expectedIds.length, 0);

    const processed = await waitForProcessing(expectedIds, (foundCount) => {
      emit("processing", foundCount, expectedIds.length, 0);
    });
    if (!processed.length) {
      setStatus("Photos still processing — try refreshing in a moment.");
      return { processed, added: 0, errors };
    }

    if (!albumId) {
      setStatus("");
      return { processed, added: 0, errors };
    }

    setStatus(`Adding ${pluralize(processed.length, "photo")} to album…`);
    emit("adding", 0, processed.length, 0);
    let result;
    try {
      result = await addToAlbum(albumId, processed, (addedCount) => {
        emit("adding", addedCount, processed.length, 0);
      });
    } catch (err) {
      setStatus(`Failed to add to album: ${err.message}`);
      return { processed, added: 0, errors };
    }
    setStatus(`Added ${pluralize(result.added, "photo")}.`);
    setTimeout(() => setStatus(""), 4000);
    return { processed, added: result.added, errors, album: result };
  }

  window.PhotoUploads = { uploadFiles };
})();
