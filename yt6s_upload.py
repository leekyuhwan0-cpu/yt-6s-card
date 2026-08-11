import os
import re
import sys
import tempfile
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from yt6s_config import ACCOUNTS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN

# 파일명 규칙: {채널접두사}_{번호}_{제목}.mp4 / .txt (예: varo_1_30 Yıl Kapı Durdurucu... .mp4)
# 제목 부분이 그대로 유튜브 업로드 제목으로 사용됨
_SHORTS_PREFIXES = ("varo", "ac", "moa", "wave")
_FNAME_RE = re.compile(r'^(?:' + "|".join(_SHORTS_PREFIXES) + r')_(\d+)_+(.+)\.(mp4|txt)$')


# ── Google Drive 인증 ─────────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def scan_drive_folder(folder_id):
    """Drive 폴더 스캔 -> 번호별로 mp4/txt + 제목 묶기.
    반환: { "1": {"mp4": {...}, "txt": {...}, "title": "..."} , ... }
    """
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    groups = {}
    for f in results.get("files", []):
        m = _FNAME_RE.match(f["name"])
        if not m:
            continue  # 이 규칙에 안 맞는 파일(기존 릴스 콘텐츠 등)은 무시
        num, title, ext = m.group(1), m.group(2), m.group(3)
        groups.setdefault(num, {"title": title})
        groups[num]["mp4" if ext == "mp4" else "txt"] = {"id": f["id"], "name": f["name"]}

    return groups


def download_from_drive(file_id, filename, tmp_dir):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fpath = os.path.join(tmp_dir, filename)
    with open(fpath, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return fpath


def delete_drive_file(file_id, filename):
    service = get_drive_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    print(f"  Drive 삭제: {filename}")


# ── YouTube Shorts 게시 ───────────────────────────────────────
def get_youtube_service(refresh_token):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)


def post_youtube_short(refresh_token, video_path, title, description, lang="tr"):
    service = get_youtube_service(refresh_token)
    body = {
        "snippet": {
            "title": (title or "Shorts")[:100],
            "description": description or "",
            "categoryId": "27",  # 교육
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,  # AI로 생성/수정된 콘텐츠임을 고지
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    print(f"  [YouTube] 게시 결과: video_id={response.get('id')}")
    return response.get("id")


# ── 메인 업로드 함수 ──────────────────────────────────────────
def post_group(channel, num, item):
    config = ACCOUNTS[channel]
    yt_refresh_token = config["youtube_refresh_token"]

    print(f"\n[{channel}] 쇼츠 '{num}' ({item['title']}) 업로드 시작")

    with tempfile.TemporaryDirectory() as tmp_dir:
        caption = ""
        if "txt" in item:
            txt_path = download_from_drive(item["txt"]["id"], item["txt"]["name"], tmp_dir)
            caption = open(txt_path, encoding="utf-8").read().strip()

        mp4_item = item["mp4"]
        fpath = download_from_drive(mp4_item["id"], mp4_item["name"], tmp_dir)

        try:
            video_id = post_youtube_short(
                yt_refresh_token, fpath, item["title"], caption, lang=config.get("lang", "tr")
            )
        except Exception as e:
            print(f"  [YouTube 오류] 예외 발생: {e}")
            return False

    if not video_id:
        print(f"  [오류] 업로드 실패")
        return False

    print(f"  [{channel}] 쇼츠 '{num}' 유튜브 업로드 완료!")

    for key in ("mp4", "txt"):
        if key in item:
            try:
                delete_drive_file(item[key]["id"], item[key]["name"])
            except Exception as e:
                print(f"  [Drive 삭제 오류] {e}")

    return True


# ── 단건 업로드 (스케줄러 호출용) ────────────────────────────
def post_one(channel, target=None):
    if channel not in ACCOUNTS:
        print(f"[{channel}] 계정 설정 없음. 현재 {list(ACCOUNTS)}만 가능합니다.")
        sys.exit(1)

    folder_id = ACCOUNTS[channel]["drive_folder_id"]
    groups = scan_drive_folder(folder_id)
    available = {num: item for num, item in groups.items() if "mp4" in item}

    if target:
        if target not in available:
            print(f"[{channel}] target '{target}' 을(를) Drive에서 찾을 수 없음")
            return
        post_group(channel, target, available[target])
        return

    if not available:
        print(f"[{channel}] 업로드 가능한 쇼츠 없음")
        return

    num = sorted(available.keys(), key=int)[0]  # 번호 오름차순으로 하나씩 처리
    post_group(channel, num, available[num])


if __name__ == "__main__":
    channel = sys.argv[1] if len(sys.argv) > 1 else "tr1"
    target = sys.argv[2] if len(sys.argv) > 2 else None
    post_one(channel, target)
