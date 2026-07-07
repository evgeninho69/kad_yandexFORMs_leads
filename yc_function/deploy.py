# Deploy kad_yandex_leads_handler to Yandex Cloud Functions via REST API.
# Steps: read IAM token, create function, upload zip, create version.

import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def call_yc(method, path, body=None, iam=None, base="https://serverless-functions.api.cloud.yandex.net"):
    url = base + path
    headers = {"Authorization": f"Bearer {iam}", "Content-Type": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YC {method} {path}: HTTP {e.code}: {text[:500]}")


def load_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def make_zip(handler_path, reqs_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with open(handler_path, "rb") as f:
            z.writestr("handler.py", f.read())
        # Если requirements.txt пустой — не кладём (YC ругается на пустой файл).
        reqs_content = open(reqs_path, "rb").read().strip()
        if reqs_content:
            z.writestr("requirements.txt", reqs_content)
    return buf.getvalue()


def main():
    env = load_env(r"C:\Users\ZotkinEA\.mavis\kad-yandex-cloud.env")
    iam_path = r"C:\Users\ZotkinEA\.mavis\yc-iam-token.txt"
    if not os.path.exists(iam_path):
        print(f"no IAM token cached at {iam_path}", file=sys.stderr)
        sys.exit(1)
    iam = open(iam_path, encoding="utf-8").read().strip()
    if not iam:
        print("IAM token file is empty", file=sys.stderr)
        sys.exit(1)

    folder_id = env["YC_FOLDER_ID"]
    func_name = env.get("YC_FUNCTION_NAME", "kad-yandex-leads-handler")

    print(f"Creating function '{func_name}' in folder {folder_id}...")
    try:
        result = call_yc(
            "POST",
            "/functions/v1/functions",
            {
                "folderId": folder_id,
                "name": func_name,
                "description": "Yandex Forms v6 webhook handler for 2KAD (Bitrix24 bridge + auto-reply via Yandex SMTP).",
            },
            iam=iam,
        )
        print(f"function created: {result.get('id', result)}")
    except RuntimeError as exc:
        if "ALREADY_EXISTS" in str(exc):
            print("function already exists, continuing")
        else:
            raise

    print("Building function zip...")
    zip_bytes = make_zip(
        r"D:\11. 2KAD_Soft\My projects\kad_yandexFORMs_leads\yc_function\handler.py",
        r"D:\11. 2KAD_Soft\My projects\kad_yandexFORMs_leads\yc_function\requirements.txt",
    )
    print(f"zip size: {len(zip_bytes)} bytes")

    body = {
        "functionId": func_name,
        "runtime": "python312",
        "entrypoint": "handler.handler",
        "resources": {"memory": str(int(env.get("YC_MEMORY_MB", "128")) * 1024 * 1024)},
        "executionTimeout": str(int(env.get("YC_TIMEOUT_SEC", "10"))) + "s",
        "package": {
            "content": base64.b64encode(zip_bytes).decode("ascii"),
            "runtime": "python312",
        },
        "environment": {
            "BITRIX_BASE_URL": env.get("BITRIX_BASE_URL", "https://bitrix.a2kad.ru"),
            "BITRIX_WEBHOOK_TOKEN": env.get("BITRIX_WEBHOOK_TOKEN", ""),
            "BITRIX_FUNNEL_ID": env.get("BITRIX_FUNNEL_ID", "3"),
            "BITRIX_RESPONSIBLE_ID": env.get("BITRIX_RESPONSIBLE_ID", "1"),
            "YANDEX_SMTP_HOST": env.get("YANDEX_SMTP_HOST", "smtp.yandex.ru"),
            "YANDEX_SMTP_PORT": env.get("YANDEX_SMTP_PORT", "587"),
            "YANDEX_SMTP_USER": env.get("YANDEX_SMTP_USER", "info@2kad.ru"),
            "YANDEX_SMTP_PASSWORD": env.get("YANDEX_SMTP_PASSWORD", ""),
            "YANDEX_SMTP_FROM_NAME": env.get("YANDEX_SMTP_FROM_NAME", "ООО Центр недвижимости 2КАД"),
            "LOG_LEVEL": env.get("LOG_LEVEL", "INFO"),
        },
    }

    print("POST /functions/v1/versions ...")
    try:
        result = call_yc("POST", "/functions/v1/versions", body, iam=iam)
    except RuntimeError as exc:
        msg = str(exc)[:600]
        print(f"create version failed: {msg}", file=sys.stderr)
        sys.exit(1)

    version_id = result.get("id", "?")
    print(f"version created: {version_id}")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:2000])

    # Если передан --publish, активируем версию.
    if "--publish" in sys.argv:
        print(f"\nPublishing version {version_id}...")
        pub = call_yc(
            "POST",
            f"/functions/v1/versions/{version_id}:publish",
            {},
            iam=iam,
        )
        pub_id = pub.get("id", "?")
        print(f"published as: {pub_id}")
        print(json.dumps(pub, indent=2, ensure_ascii=False)[:1000])

    # Покажем актуальную функцию с URL.
    print("\nFetching function metadata...")
    try:
        meta = call_yc("GET", f"/functions/v1/functions?folderId={folder_id}", iam=iam)
        for f in meta.get("functions", []):
            if f.get("name") == func_name:
                print(f"  Function: {f.get('id')}")
                print(f"  Status: {f.get('status')}")
                print(f"  http_invoke_url: {f.get('http_invoke_url', '(not set — create API gateway)')}")
                break
    except Exception as exc:
        print(f"meta fetch skipped: {exc}")


if __name__ == "__main__":
    main()
