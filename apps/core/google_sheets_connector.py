import os
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _extract_id_from_url(url, marker):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if marker in parts:
        index = parts.index(marker)
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def extract_drive_folder_id(value):
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        folder_id = _extract_id_from_url(value, "folders")
        if folder_id:
            return folder_id
        parsed = urlparse(value)
        query_id = parse_qs(parsed.query).get("id", [])
        if query_id:
            return query_id[0]
    return value


def extract_spreadsheet_id(value):
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        spreadsheet_id = _extract_id_from_url(value, "d")
        if spreadsheet_id:
            return spreadsheet_id
        parsed = urlparse(value)
        query_id = parse_qs(parsed.query).get("id", [])
        if query_id:
            return query_id[0]
    return value


def get_service_account_credentials(scopes=None):
    import google.auth
    from google.oauth2 import service_account

    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    requested_scopes = scopes or [SHEETS_READONLY_SCOPE]

    # Cloud runtimes can use Application Default Credentials from the attached
    # service account without mounting a JSON key file.
    if not credentials_path:
        credentials, _ = google.auth.default(scopes=requested_scopes)
        return credentials

    credentials_file = Path(credentials_path).expanduser()
    if not credentials_file.exists():
        raise ValueError(f"GOOGLE_APPLICATION_CREDENTIALS does not exist: {credentials_file}")

    return service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=requested_scopes,
    )


def build_google_service(service_name, version, scopes):
    from googleapiclient.discovery import build

    credentials = get_service_account_credentials(scopes=scopes)
    return build(service_name, version, credentials=credentials, cache_discovery=False)


def list_spreadsheets_in_folder(folder_id, drive_service):
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and mimeType='{SPREADSHEET_MIME_TYPE}' and trashed=false"

    while True:
        response = (
            drive_service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, modifiedTime)",
                orderBy="name",
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return files


def list_child_folder_ids(folder_id, drive_service):
    ids = []
    page_token = None
    query = f"'{folder_id}' in parents and mimeType='{FOLDER_MIME_TYPE}' and trashed=false"

    while True:
        response = (
            drive_service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id)",
                orderBy="name",
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("files", []):
            ids.append(item["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return ids


def find_spreadsheet_by_name_in_folder_tree(drive_service, root_folder_id, spreadsheet_name):
    """Breadth-first search for a spreadsheet with exact *spreadsheet_name* under *root_folder_id*."""
    queue = deque([root_folder_id])
    seen_folders = set()
    matches = []

    while queue:
        fid = queue.popleft()
        if fid in seen_folders:
            continue
        seen_folders.add(fid)

        for item in list_spreadsheets_in_folder(fid, drive_service):
            if item.get("name") == spreadsheet_name:
                if not any(m.get("id") == item.get("id") for m in matches):
                    matches.append(item)

        for child_id in list_child_folder_ids(fid, drive_service):
            if child_id not in seen_folders:
                queue.append(child_id)

    if len(matches) > 1:
        ids_preview = ", ".join(m["id"] for m in matches[:5])
        raise ValueError(
            f"multiple spreadsheets named {spreadsheet_name!r} under folder {root_folder_id} "
            f"(recursive search); ids: {ids_preview}"
        )
    return matches[0] if matches else None


def resolve_spreadsheet(tab, drive_service=None, folder_id=None, search_descendants=False):
    spreadsheet_id = extract_spreadsheet_id(tab.get("spreadsheet_id") or tab.get("spreadsheet_url"))
    if spreadsheet_id:
        return {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_name": tab.get("spreadsheet_name") or spreadsheet_id,
            "modified_time": None,
        }

    spreadsheet_name = tab.get("spreadsheet_name")
    if not spreadsheet_name or not folder_id or drive_service is None:
        raise ValueError(
            "tab entry must provide spreadsheet_id/spreadsheet_url or spreadsheet_name with drive folder access"
        )

    if search_descendants:
        match = find_spreadsheet_by_name_in_folder_tree(drive_service, folder_id, spreadsheet_name)
        if match is None:
            raise ValueError(
                f"spreadsheet named '{spreadsheet_name}' not found under folder {folder_id} (recursive search)"
            )
    else:
        matches = [
            item
            for item in list_spreadsheets_in_folder(folder_id, drive_service)
            if item.get("name") == spreadsheet_name
        ]
        if not matches:
            raise ValueError(f"spreadsheet named '{spreadsheet_name}' not found in folder {folder_id}")
        if len(matches) > 1:
            raise ValueError(f"multiple spreadsheets named '{spreadsheet_name}' found in folder {folder_id}")

        match = matches[0]
    return {
        "spreadsheet_id": match["id"],
        "spreadsheet_name": match["name"],
        "modified_time": match.get("modifiedTime"),
    }


def fetch_tab_rows(spreadsheet_id, worksheet_title, sheets_service):
    range_name = worksheet_title.replace("'", "''")
    response = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{range_name}'")
        .execute()
    )
    return response.get("values", [])
