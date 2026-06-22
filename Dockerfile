FROM public.ecr.aws/lambda/python:3.13

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html photo.html albums.html album.html collections.html collection.html public_album.html public_collection.html public_photo.html login.html login_sent.html uploads.js ${LAMBDA_TASK_ROOT}/
COPY database ${LAMBDA_TASK_ROOT}/database/

CMD ["app.handler"]
