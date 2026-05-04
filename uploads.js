(function () {
  const ACCEPT = new Set(["image/jpeg", "image/png", "image/webp"]);
  const POLL_TIMEOUT_MS = 60_000;
  const POLL_INTERVAL_MS = 1500;

  function pluralize(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
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

  async function waitForProcessing(ids) {
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
      } catch (_e) {}
    }
    return Array.from(found);
  }

  async function addToAlbum(albumId, photoIds) {
    const resp = await fetch(
      `/api/albums/${encodeURIComponent(albumId)}/photos`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo_ids: photoIds }),
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
    return resp.json();
  }

  async function uploadFiles(files, opts = {}) {
    const { albumId, onStatus } = opts;
    const setStatus = (t) => { if (onStatus) onStatus(t || ""); };

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
    let done = 0;
    const expectedIds = [];
    const errors = [];
    setStatus(`Uploading 0/${total}…`);

    await Promise.all(accepted.map(async (f, i) => {
      try {
        await putOne(f, uploads[i]);
        expectedIds.push(uploads[i].photo_id);
        done++;
        setStatus(`Uploading ${done}/${total}…`);
      } catch (e) {
        errors.push(e);
      }
    }));

    if (!expectedIds.length) {
      setStatus(`Uploads failed: ${errors.length} error(s).`);
      return { processed: [], added: 0, errors };
    }

    if (errors.length) {
      setStatus(`Uploaded ${done}/${total}. ${errors.length} failed. Processing…`);
    } else {
      setStatus(`Uploaded ${total}. Processing…`);
    }

    const processed = await waitForProcessing(expectedIds);
    if (!processed.length) {
      setStatus("Photos still processing — try refreshing in a moment.");
      return { processed, added: 0, errors };
    }

    if (!albumId) {
      setStatus("");
      return { processed, added: 0, errors };
    }

    setStatus(`Adding ${pluralize(processed.length, "photo")} to album…`);
    let result;
    try {
      result = await addToAlbum(albumId, processed);
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
