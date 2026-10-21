"""명조 굿즈 도감 로컬 서버

index.html을 띄우고, JSON 생성 탭의 "프로젝트 폴더에 저장"과 캐릭터 탭의 추가·수정·삭제, 추가 탭의 시리즈·굿즈 저장,
굿즈 탭의 굿즈 수정·삭제, 올리기 탭의 커밋·푸시를 받아 준다.

goods.json을 저장할 때 굿즈마다 바뀌지 않는 번호(goodsUid)를 붙인다. 번호는 goods_ids.json에 사진 경로별로 적어 두고,
굿즈 탭에서 이름을 바꾸면 번호도 새 경로로 옮긴다. 탐색기에서 직접 이름을 바꾼 굿즈는 새 굿즈로 보고 새 번호를 받는다. 올리기 탭은 푸시 전에 빠진 번호를 채운다.

사용법:
    python serve.py            # http://127.0.0.1:8765
    python serve.py 9000       # 포트 지정
    pythonw serve.py --no-open # 창 없이 실행 (Windows 시작 시 자동 실행용)
"""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# 저장을 허용하는 파일 (이 외의 이름은 거부)
SAVE_FILES = ("series.json", "goods.json", "categories.json")

RAW_PREFIX = "https://raw.githubusercontent.com/rhecpev/wuwa-goods-data/refs/heads/main/"
BAD_NAME_CHARS = '_,/\\:*?"<>|.'
GOODS_KEY = ("goodsSeries", "goodsReleaseDate", "goodsChara", "goodsCategory", "goodsPrice", "goodsMemo")
MAX_BODY = 60 * 1024 * 1024
MAX_UPLOAD = 1024 * 1024 * 1024

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif")
BAD_PATH_CHARS = '/\\:*?"<>|'

# 굿즈 번호 대장. {"next": 다음에 줄 번호, "ids": {"시리즈폴더/goods/파일명": 번호}}
# 지운 굿즈의 번호는 다시 쓰지 않는다. 앱의 옛 버전이 goodsId를 읽을 수 있어서 번호는 goodsUid 칸에 따로 둔다.
IDS_FILE = "goods_ids.json"
PICTURE_PREFIX = RAW_PREFIX + "picture/"
ids_lock = threading.Lock()


def json_text(data):
    """index.html의 JSON.stringify(data, null, 2)와 바이트 단위로 같은 CRLF 텍스트"""
    return json.dumps(data, ensure_ascii=False, indent=2).replace("\n", "\r\n")


def load_ids(directory):
    path = os.path.join(directory, IDS_FILE)
    if not os.path.isfile(path):
        return {"next": 1, "ids": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ids(directory, registry):
    # 다 쓴 뒤에 바꿔 끼워서, 중간에 실패해도 대장이 깨지지 않는다
    path = os.path.join(directory, IDS_FILE)
    with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
        f.write(json_text(registry))
    os.replace(path + ".tmp", path)


def with_uids(goods, registry):
    """굿즈마다 goodsUid를 goodsId 바로 뒤에 붙인다. 대장에 없는 굿즈는 새 번호를 받는다."""
    ids = registry["ids"]
    result = []
    for item in goods:
        url = str(item.get("goodsUrl", ""))
        path = url[len(PICTURE_PREFIX):] if url.startswith(PICTURE_PREFIX) else url
        uid = ids.get(path)
        if not uid:
            uid = registry["next"]
            registry["next"] += 1
            ids[path] = uid
        ordered = {} if "goodsId" in item else {"goodsUid": uid}
        for key, value in item.items():
            if key == "goodsUid":
                continue
            ordered[key] = value
            if key == "goodsId":
                ordered["goodsUid"] = uid
        result.append(ordered)
    return result


def bad_path_part(value):
    """폴더명·파일명으로 쓸 수 없는 값인지"""
    if not value or value != value.strip() or value.endswith("."):
        return True
    return any(ch in BAD_PATH_CHARS or ord(ch) < 32 for ch in value)


def goods_content(item, valid_characters):
    """굿즈 정보로 확장자를 뺀 파일명(캐릭터_카테고리_가격_메모)을 만든다. (파일명, 오류)"""
    charas = [c.strip() for c in str(item.get("chara", "")).split(",") if c.strip()]
    category = str(item.get("category", "")).strip()
    price = str(item.get("price", "")).strip()
    memo = str(item.get("memo", "")).strip()
    ext = str(item.get("ext", "")).lower()

    missing = [c for c in charas if c not in valid_characters]
    if not charas or missing:
        return None, f"등록되지 않은 캐릭터: {', '.join(missing) or '(없음)'}"
    if not category or not price or "_" in category or "_" in price:
        return None, f"카테고리·가격이 비었거나 _ 가 들어 있습니다: {category} / {price}"
    if ext not in IMAGE_EXTS:
        return None, f"이미지 형식을 쓸 수 없습니다: {ext}"

    content = "_".join([",".join(charas), category, price] + ([memo] if memo else []))
    if bad_path_part(content + ext):
        return None, f"파일명에 쓸 수 없는 글자가 있습니다: {content + ext}"
    return content, None


class Handler(SimpleHTTPRequestHandler):

    def end_headers(self):
        # JSON, 폴더 목록, 캐릭터 이미지는 항상 최신으로 읽게 한다 (굿즈 이미지는 캐시 허용)
        path = self.path.split("?")[0]
        if path.endswith(".json") or path.endswith("/") or path.startswith(("/api/", "/character/")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/ping":
            self.reply(200, {"save": True, "files": list(SAVE_FILES)})
            return
        if self.path == "/api/git/status":
            self.git_status()
            return
        super().do_GET()

    def do_POST(self):
        if self.path not in ("/api/save", "/api/character", "/api/goods", "/api/goods/edit", "/api/git/push"):
            self.reply(404, {"error": "없는 주소"})
            return

        # 다른 사이트가 몰래 호출하지 못하게, 교차 출처로는 붙일 수 없는 헤더를 요구한다
        if self.headers.get("X-Goods-Save") != "1":
            self.reply(403, {"error": "허용되지 않은 요청"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > (MAX_UPLOAD if self.path == "/api/goods" else MAX_BODY):
                raise ValueError("요청이 너무 큽니다")
            raw = self.rfile.read(length)
            # 굿즈 추가는 [정보 길이 4바이트][정보 JSON][이미지들] 순서의 바이너리로 온다
            body = None if self.path == "/api/goods" else json.loads(raw.decode("utf-8"))
        except Exception as e:
            self.reply(400, {"error": f"요청 오류: {e}"})
            return

        if self.path == "/api/save":
            self.save_json(body)
        elif self.path == "/api/character":
            self.character(body)
        elif self.path == "/api/goods":
            self.add_goods(raw)
        elif self.path == "/api/goods/edit":
            self.edit_goods(body)
        else:
            self.git_push(body)

    def save_json(self, body):
        files = body.get("files") or {}

        # 하나라도 이상하면 아무것도 쓰지 않는다
        goods = None
        for name, text in files.items():
            if name not in SAVE_FILES:
                self.reply(400, {"error": f"저장할 수 없는 파일: {name}"})
                return
            try:
                data = json.loads(text)
            except Exception as e:
                self.reply(400, {"error": f"{name} 내용 오류: {e}"})
                return
            if name == "goods.json":
                goods = data

            # 한 시리즈 안에 캐릭터·카테고리·가격·메모가 전부 같은 굿즈가 있으면 안 된다
            if name == "goods.json":
                seen = set()
                for item in data:
                    key = tuple(str(item.get(k, "")).lower() for k in GOODS_KEY)
                    if key in seen:
                        self.reply(400, {"error": "내용이 완전히 같은 굿즈가 있습니다: " + " / ".join(key)})
                        return
                    seen.add(key)

        if goods is not None:
            with ids_lock:
                registry = load_ids(self.directory)
                files = {**files, "goods.json": json_text(with_uids(goods, registry))}
                save_ids(self.directory, registry)

        for name, text in files.items():
            # newline=""로 줄바꿈(CRLF)을 보낸 그대로 쓴다
            with open(os.path.join(self.directory, name), "w", encoding="utf-8", newline="") as f:
                f.write(text)
            print(f"저장: {name}")

        self.reply(200, {"saved": list(files)})

    def character_image(self, item):
        """캐릭터 이미지 파일 경로. character 폴더의 png가 아니면 None"""
        prefix = RAW_PREFIX + "character/"
        url = item.get("charaUrl", "")
        filename = url[len(prefix):]
        if not url.startswith(prefix) or not filename.lower().endswith(".png"):
            return None
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return None
        return os.path.join(self.directory, "character", filename)

    def character(self, body):
        action = body.get("action", "add")
        name = str(body.get("name", "")).strip()

        characters_file = os.path.join(self.directory, "characters.json")
        with open(characters_file, "r", encoding="utf-8", newline="") as f:
            text = f.read()

        # 한 줄에 한 명씩 적힌 모양을 그대로 두고, 해당 줄만 넣고 고치고 뺀다
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split(newline)
        rows = [i for i, line in enumerate(lines) if line.strip().startswith("{")]
        try:
            items = [json.loads(lines[i].strip().rstrip(",")) for i in rows]
            if items != json.loads(text):
                raise ValueError("한 줄에 한 명 형식이 아님")
        except Exception as e:
            self.reply(500, {"error": f"characters.json 형식을 알 수 없습니다: {e}"})
            return
        names = [item.get("charaNm", "").strip() for item in items]

        image = None
        if body.get("image"):
            try:
                image = base64.b64decode(body["image"], validate=True)
            except Exception:
                image = b""
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                self.reply(400, {"error": "PNG 이미지가 아닙니다"})
                return

        def bad_name(value):
            # 굿즈 파일명이 캐릭터_카테고리_가격 / 콤마 구분이라 이 글자들은 이름에 쓸 수 없다
            return not value or any(ch in value for ch in BAD_NAME_CHARS)

        write_image = None
        remove_image = None

        if action == "add":
            if bad_name(name):
                self.reply(400, {"error": f"쓸 수 없는 이름: '{name}'"})
                return
            if name in names:
                self.reply(400, {"error": f"이미 등록된 캐릭터: {name}"})
                return
            if image is None:
                self.reply(400, {"error": "이미지가 없습니다"})
                return

            filename = name.replace(" ", "-") + ".png"
            write_image = os.path.join(self.directory, "character", filename)
            if os.path.exists(write_image):
                self.reply(400, {"error": f"이미 있는 이미지 파일: character/{filename}"})
                return

            entry = {
                "charaId": 0,
                "charaNm": name,
                "charaUrl": RAW_PREFIX + "character/" + filename,
                "charaIsFavorite": False,
            }
            lines.insert(rows[0] if rows else 1, "  " + json.dumps(entry, ensure_ascii=False))
            result = {"added": name}

        elif action in ("update", "delete"):
            if name not in names:
                self.reply(404, {"error": f"없는 캐릭터: {name}"})
                return
            index = names.index(name)
            item = items[index]
            image_file = self.character_image(item)

            if action == "update":
                new_name = str(body.get("newName", name)).strip()
                if new_name != name:
                    if bad_name(new_name):
                        self.reply(400, {"error": f"쓸 수 없는 이름: '{new_name}'"})
                        return
                    if new_name in names:
                        self.reply(400, {"error": f"이미 등록된 캐릭터: {new_name}"})
                        return
                if image is not None:
                    # 주소가 바뀌지 않게 지금 쓰는 파일을 그대로 덮어쓴다
                    if image_file is None:
                        self.reply(400, {"error": "이 캐릭터의 이미지 경로를 알 수 없어 바꿀 수 없습니다"})
                        return
                    write_image = image_file

                item["charaNm"] = new_name
                lines[rows[index]] = "  " + json.dumps(item, ensure_ascii=False)
                result = {"updated": new_name}
            else:
                del lines[rows[index]]
                others = [other.get("charaUrl") for i, other in enumerate(items) if i != index]
                if image_file and item.get("charaUrl") not in others and os.path.isfile(image_file):
                    remove_image = image_file
                result = {"deleted": name}

        else:
            self.reply(400, {"error": f"알 수 없는 동작: {action}"})
            return

        # 마지막 줄만 쉼표가 없도록 다시 맞춘다
        rows = [i for i, line in enumerate(lines) if line.strip().startswith("{")]
        for n, i in enumerate(rows):
            lines[i] = lines[i].rstrip().rstrip(",") + ("," if n < len(rows) - 1 else "")
        text = newline.join(lines)

        try:
            json.loads(text)
        except Exception as e:
            self.reply(500, {"error": f"characters.json을 고치지 못했습니다: {e}"})
            return

        if write_image:
            os.makedirs(os.path.dirname(write_image), exist_ok=True)
            with open(write_image, "wb") as f:
                f.write(image)
            result["image"] = "character/" + os.path.basename(write_image)
        with open(characters_file, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if remove_image:
            os.remove(remove_image)
            result["removedImage"] = "character/" + os.path.basename(remove_image)

        print(f"캐릭터 {action}: {result}")
        self.reply(200, result)


    def valid_characters(self):
        with open(os.path.join(self.directory, "characters.json"), "r", encoding="utf-8") as f:
            return {item["charaNm"].strip() for item in json.load(f) if item.get("charaNm")}

    def edit_goods(self, body):
        """굿즈 하나의 파일명(=정보)을 바꾸거나 사진을 바꾸거나 파일을 지운다"""
        action = body.get("action")
        parts = str(body.get("path", "")).split("/")
        if len(parts) != 3 or parts[1] != "goods" or any(bad_path_part(part) for part in parts):
            self.reply(400, {"error": f"굿즈 경로가 이상합니다: {body.get('path')}"})
            return
        folder, _, filename = parts
        goods_dir = os.path.join(self.directory, "picture", folder, "goods")
        old_path = os.path.join(goods_dir, filename)
        if not os.path.isfile(old_path):
            self.reply(404, {"error": f"없는 굿즈 파일: {folder}/goods/{filename} (JSON을 저장한 뒤 다시 열어 보세요)"})
            return

        if action == "delete":
            os.remove(old_path)
            print(f"굿즈 삭제: {folder}/goods/{filename}")
            self.reply(200, {"deleted": filename})
            return
        if action != "update":
            self.reply(400, {"error": f"알 수 없는 동작: {action}"})
            return

        image = None
        if body.get("image"):
            try:
                image = base64.b64decode(body["image"], validate=True)
            except Exception:
                image = b""
            if not image:
                self.reply(400, {"error": "사진을 읽지 못했습니다"})
                return
            ext = str(body.get("ext", "")).lower()
        else:
            ext = os.path.splitext(filename)[1].lower()

        content, error = goods_content({**body, "ext": ext}, self.valid_characters())
        if error:
            self.reply(400, {"error": error})
            return
        new_name = content + ext
        if new_name == filename and image is None:
            self.reply(400, {"error": "바뀐 것이 없습니다"})
            return

        # 확장자만 다른 것도 같은 굿즈로 본다. 자기 자신은 빼고 비교한다
        for name in os.listdir(goods_dir):
            if name != filename and os.path.splitext(name)[0].lower() == content.lower():
                self.reply(400, {"error": f"내용이 완전히 같은 굿즈가 이미 있습니다: {name}"})
                return

        new_path = os.path.join(goods_dir, new_name)
        try:
            if image is None:
                os.rename(old_path, new_path)
            else:
                # 새 사진을 다 쓴 뒤에 옛 파일을 지워서, 중간에 실패해도 원래 사진은 남는다
                temp_path = new_path + ".tmp"
                with open(temp_path, "wb") as f:
                    f.write(image)
                if new_name.lower() != filename.lower():
                    os.remove(old_path)
                os.replace(temp_path, new_path)
        except Exception as e:
            self.reply(500, {"error": f"파일 저장 실패: {e}"})
            return

        # 이름이 바뀌어도 같은 굿즈이므로 번호를 새 경로로 옮긴다
        if new_name != filename:
            with ids_lock:
                registry = load_ids(self.directory)
                uid = registry["ids"].pop(f"{folder}/goods/{filename}", None)
                if uid:
                    registry["ids"][f"{folder}/goods/{new_name}"] = uid
                    save_ids(self.directory, registry)

        print(f"굿즈 수정: {folder}/goods/{filename} → {new_name}")
        self.reply(200, {"renamed": filename, "name": new_name})

    def add_goods(self, raw):
        try:
            (meta_length,) = struct.unpack(">I", raw[:4])
            meta = json.loads(raw[4:4 + meta_length].decode("utf-8"))
            data = raw[4 + meta_length:]
            series = meta["series"]
            goods = meta.get("goods") or []
            main = meta.get("mainTheme")
        except Exception as e:
            self.reply(400, {"error": f"요청 오류: {e}"})
            return

        picture_dir = os.path.join(self.directory, "picture")

        # 시리즈: 기존 폴더에 추가하거나, 새 폴더(시리즈명_국가_날짜)를 만든다
        is_new = not series.get("folder")
        if is_new:
            name = str(series.get("name", "")).strip()
            country = str(series.get("country", "")).strip()
            date = str(series.get("date", "")).strip()
            if bad_path_part(name) or bad_path_part(country) or bad_path_part(date) or "_" in country or "_" in date:
                self.reply(400, {"error": "시리즈 이름·국가·날짜에 쓸 수 없는 글자가 있습니다"})
                return
            folder = f"{name}_{country}_{date}"
            if os.path.exists(os.path.join(picture_dir, folder)):
                self.reply(400, {"error": f"이미 있는 시리즈 폴더: {folder}"})
                return
            if not main:
                self.reply(400, {"error": "새 시리즈는 대표 이미지가 필요합니다"})
                return
        else:
            folder = str(series["folder"])
            if bad_path_part(folder) or not os.path.isdir(os.path.join(picture_dir, folder)):
                self.reply(400, {"error": f"없는 시리즈 폴더: {folder}"})
                return
            main = None

        if not goods:
            self.reply(400, {"error": "굿즈가 없습니다"})
            return

        valid_characters = self.valid_characters()
        folder_path = os.path.join(picture_dir, folder)
        goods_dir = os.path.join(folder_path, "goods")
        files = []  # (저장 경로, 크기)

        if main:
            ext = str(main.get("ext", "")).lower()
            if ext not in IMAGE_EXTS:
                self.reply(400, {"error": f"대표 이미지 형식을 쓸 수 없습니다: {ext}"})
                return
            files.append((os.path.join(folder_path, "mainTheme" + ext), int(main["size"])))

        # 확장자만 다른 것도 같은 굿즈로 본다
        seen = set()
        existing = set()
        if os.path.isdir(goods_dir):
            existing = {os.path.splitext(name)[0].lower() for name in os.listdir(goods_dir)}
        for item in goods:
            content, error = goods_content(item, valid_characters)
            if error:
                self.reply(400, {"error": error})
                return
            filename = content + str(item.get("ext", "")).lower()
            if content.lower() in seen or content.lower() in existing:
                self.reply(400, {"error": f"내용이 완전히 같은 굿즈가 이미 있습니다: {content}"})
                return
            seen.add(content.lower())
            files.append((os.path.join(goods_dir, filename), int(item["size"])))

        if sum(size for _, size in files) != len(data) or any(size <= 0 for _, size in files):
            self.reply(400, {"error": "이미지 크기가 맞지 않습니다"})
            return

        # 전부 확인한 뒤에 쓴다. 쓰다가 실패하면 이번에 만든 것은 지운다
        written = []
        try:
            os.makedirs(goods_dir, exist_ok=True)
            offset = 0
            for file_path, size in files:
                with open(file_path, "xb") as f:
                    f.write(data[offset:offset + size])
                written.append(file_path)
                offset += size
        except Exception as e:
            for file_path in written:
                os.remove(file_path)
            if is_new:
                for directory in (goods_dir, folder_path):
                    if os.path.isdir(directory) and not os.listdir(directory):
                        os.rmdir(directory)
            self.reply(500, {"error": f"파일 저장 실패: {e}"})
            return

        print(f"굿즈 추가: {folder} ({len(goods)}개)")
        self.reply(200, {"folder": folder, "created": is_new, "goods": len(goods)})

    def git(self, *args):
        # 창 없이 실행하고, 로그인 입력을 기다리며 멈추지 않게 한다
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=self.directory,
            capture_output=True,
            timeout=900,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")

    def git_status(self):
        try:
            code, out = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
            if code != 0:
                raise RuntimeError(out.strip())
            _, branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
            ahead_code, ahead = self.git("rev-list", "--count", "@{u}..HEAD")
        except Exception as e:
            self.reply(500, {"error": f"git 상태를 읽지 못했습니다: {e}"})
            return

        changes = []
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            code, file_path = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                i += 1  # 이름이 바뀐 파일은 원래 경로가 한 칸 더 온다
            if code == "??" or "A" in code:
                kind = "added"
            elif "D" in code:
                kind = "deleted"
            else:
                kind = "modified"
            changes.append({"kind": kind, "path": file_path})

        self.reply(200, {
            "branch": branch.strip(),
            "ahead": int(ahead.strip()) if ahead_code == 0 else None,
            "changes": changes,
        })

    def git_push(self, body):
        message = str(body.get("message", "")).strip()
        if not message:
            self.reply(400, {"error": "커밋 메시지가 없습니다"})
            return

        log = []

        def run(*args):
            code, out = self.git(*args)
            log.append("$ git " + " ".join(args) + "\n" + out.strip())
            return code

        try:
            # exe로 만든 goods.json을 복사해 넣었으면 번호가 빠져 있으니, 올리기 전에 대장에서 채운다
            goods_path = os.path.join(self.directory, "goods.json")
            with ids_lock:
                with open(goods_path, "r", encoding="utf-8") as f:
                    goods = json.load(f)
                if any(not item.get("goodsUid") for item in goods):
                    registry = load_ids(self.directory)
                    with open(goods_path, "w", encoding="utf-8", newline="") as f:
                        f.write(json_text(with_uids(goods, registry)))
                    save_ids(self.directory, registry)
                    log.append("goods.json에 빠진 굿즈 번호를 채웠습니다")
            if run("add", "-A") != 0:
                raise RuntimeError("git add 실패")
            # 올릴 변경이 있을 때만 커밋한다 (지난번에 커밋만 되고 푸시가 실패했으면 푸시만)
            staged, _ = self.git("diff", "--cached", "--quiet")
            if staged != 0 and run("commit", "-q", "-m", message) != 0:
                raise RuntimeError("git commit 실패")
            if run("push") != 0:
                raise RuntimeError("git push 실패")
        except Exception as e:
            self.reply(500, {"error": str(e), "log": "\n\n".join(log)})
            return

        print(f"푸시: {message}")
        self.reply(200, {"pushed": True, "log": "\n\n".join(log)})


if __name__ == "__main__":

    # pythonw로 창 없이 실행하면 출력할 곳이 없어서 요청 로그에서 오류가 난다
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    port = int(args[0]) if len(args) > 0 else 8765
    root_dir = os.path.abspath(args[1]) if len(args) > 1 else os.path.dirname(os.path.abspath(sys.argv[0]))

    if not os.path.isdir(os.path.join(root_dir, "picture")):
        print(f"폴더 없음: {os.path.join(root_dir, 'picture')}")
        sys.exit(1)

    url = f"http://127.0.0.1:{port}"

    # 이미 켜져 있으면 페이지만 연다 (Windows는 같은 포트에 두 번 열려도 오류가 안 나서 직접 확인)
    with socket.socket() as probe:
        probe.settimeout(0.5)
        running = probe.connect_ex(("127.0.0.1", port)) == 0

    if running:
        print(f"이미 실행 중: {url}")
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        sys.exit(0)

    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=root_dir))

    print("=" * 60)
    print(f"프로젝트 폴더: {root_dir}")
    print(f"주소: {url}")
    print("종료: Ctrl+C")
    print("=" * 60)

    if "--no-open" not in sys.argv:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
