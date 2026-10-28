# 基底映像釘 digest，套件照 requirements.lock 的版本安裝並驗雜湊，同一份原始碼每次建出來的環境都一樣。
# 換新的基底映像：docker buildx imagetools inspect python:3.12-slim，取最上面的 Digest。
# 換套件版本：改 requirements.txt 後，照 requirements.lock 檔頭那行指令重新產生。
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY . .
RUN useradd -u 10001 -m appuser && mkdir -p /data && chown appuser /data
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
