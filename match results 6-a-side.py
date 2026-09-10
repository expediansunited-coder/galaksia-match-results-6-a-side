# ============================================================
# match results 6-a-side.py
# Combined: Match Results + MotM generation & posting (carousel Reel + Story)
# ============================================================
import io
import os
import re
import sys
import json
import time
import random
import unicodedata
import difflib
from collections import deque
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception as e:
    print('  [heic] pillow-heif not available: %s' % e)

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()   # lets Pillow open .heic/.heif
except Exception as _e:
    print('  [heic] pillow-heif not available: %s' % _e)

try:
    from rembg import remove
except ImportError:
    def remove(data):
        return data

try:
    import fitz  # PyMuPDF, for SVG flags
except ImportError:
    fitz = None

# ============================================================
# CONFIG
# ============================================================
CREDENTIALS_FILE = 'credentials.json'
META_CONFIG_FILE = 'meta_config_6aside.json'

RESP_SS_ID = '1T4VG3O1Zn56PNaYtwtwWxuaDT920UnTcDRNZxXEmODA'
FIX_SS_ID = '1j6ZN3N8aXnB9vKFdWeXhY-fyo8aH1JlmhWZWHwzgu-E'
PERSONAL_INFO_SS_ID = '1XVwxahQCx6DkVceeQn7nvvukobHgOMmVGlDA7w7MgyM'
PERSONAL_INFO_TAB = 'Personal Info'

RESP_TABS = ['6A', '6B', '6C', '6D', 'VETs']

ASSETS_FOLDER_ID = '1-MAJwpIAjQvzXQdsPdqmkX4NGrM8YFt5'
LOGOS_FOLDER_ID = '19NNyf1trl1LoA7Tth7PFMbRAv65oXeeR'
PLAYER_PHOTOS_ROOT_FOLDER_ID = '1xyUCnKlA_AQUU2g99y8D4jb2Yr4MOd1s'
FLAGS_FOLDER_ID = '1a86OhVTr1hr93ri2w67o7-nGZishLLS2'

BACKGROUND_DRIVE_NAME = '6-a-side Match Results'
MOTM_BACKGROUND_FILE_STEM = '6-a-side Motm'
FONT_NAME = 'Etna'
_FONT_LOCAL = '_etna.ttf'
MOTM_FONT_LOCAL = 'horta.otf'  # expected to already exist locally (as in original script)

INDEX_TAB = 'Index'
IDX_COL_TEAM = 0
IDX_COL_LEAGUE = 2

OUTPUT_DIR = 'output'
GITHUB_REPO_RAW_BASE = ('https://raw.githubusercontent.com/'
                         'expediansunited-coder/galaksia-match-results-6-a-side/main/')

# ---- Match Results canvas config (unchanged from original script) ----
CANVAS_W = 768
CANVAS_H = 960
BAR_TOP = 455
BAR_BOTTOM = 620
BAR_CENTER_Y = (BAR_TOP + BAR_BOTTOM) // 2
HOME_LOGO_CENTER = (110, BAR_CENTER_Y)
AWAY_LOGO_CENTER = (660, BAR_CENTER_Y)
LOGO_MAX = 150
AWAY_NAME_CY = BAR_BOTTOM - 18
AWAY_NAME_MAX_W = 220
AWAY_NAME_SIZE = 26
SCORE_HOME_X = 330
SCORE_AWAY_X = 438
SCORE_Y = 530
SCORE_FONT_SIZE = 135
SCORERS_MIN_X = 20
SCORERS_NAME_X = 120
SCORERS_START_Y = 675
SCORERS_LINE_H = 46
SCORERS_FONT_SIZE = 30
LEAGUE_LOGO_CENTER = (690, 875)
LEAGUE_LOGO_MAX = 110
LABEL_Y = 945
LABEL_FONT_SIZE = 26
FRIENDLY_RIGHT_X = CANVAS_W - 30
FRIENDLY_Y = 890
FRIENDLY_FONT_SIZE = 40
PLAYER_CENTER_X = 384
PLAYER_TOP_Y = 120
PLAYER_BOTTOM_Y = 960
PLAYER_FADE_FRAC = 0.35
TEXT_COLOR = (255, 255, 255)

GALAKSIA_TEAM_CODES = ('6a', '6b', '6c', '6d', 'vets', 'vet', '11a', '11b', '11c', 'bba', 'bbb')
GALAKSIA_LOGO_NAME = 'galaksia praha 23'
NO_LOGO_NAME = 'no logo'
IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif')

# ---- MotM canvas config (unchanged from original script) ----
NAME_FONT_SIZE_MAX = 140
NAME_TARGET_WIDTH_RATIO = 0.60
FLAG_TOP_MARGIN_RATIO = 0.035
FLAG_ALPHA_FACTOR = 0.22
PHOTO_HEIGHT_RATIO = 0.95
PHOTO_TOP_CROP_FRACTION = 5 / 9

# ---- Story dimensions (from lineup script) ----
STORY_W = 1080
STORY_H = 1920
GRAPH = 'https://graph.facebook.com/v20.0'

# ============================================================
# AUTH
# ============================================================
def get_creds():
    scope = ['https://www.googleapis.com/auth/spreadsheets',
             'https://www.googleapis.com/auth/drive']
    return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)

def get_gspread_client():
    return gspread.authorize(get_creds())

def get_drive_service():
    return build('drive', 'v3', credentials=get_creds())

def load_meta_config():
    with open(META_CONFIG_FILE) as f:
        return json.load(f)

# ============================================================
# API RETRY (from lineup script)
# ============================================================
from gspread.exceptions import APIError

def with_retry(func, *args, retries=6, base_delay=3, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            code = None
            try:
                code = e.response.status_code
            except Exception:
                pass
            transient = code in (429, 500, 502, 503) or code is None
            if not transient or attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print('  [retry] API %s - attempt %d/%d, waiting %.1fs'
                  % (code, attempt + 1, retries, delay))
            time.sleep(delay)

def send_error_email(errors):
    if not errors:
        return
    print('  [errors] %d issue(s):' % len(errors))
    for e in errors:
        print('    - %s' % e)
    sys.exit(1)

# ============================================================
# GENERIC HELPERS
# ============================================================
def _norm(s):
    if not s: return ''
    s = unicodedata.normalize('NFKD', str(s))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())

def _tokens(s):
    if not s: return []
    s = unicodedata.normalize('NFKD', str(s))
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    return [t for t in re.split(r'[^a-z0-9]+', s) if t]

def clean_team_name(name):
    s = (name or '').strip()
    return re.sub(r'\s*,?\s*(z\.s\.|a\.s\.)\s*$', '', s, flags=re.I).strip()

def normalize_fuzzy(s):
    """Used by MotM fuzzy-matching (accent/case/punct-insensitive)."""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()

def strip_extension(filename):
    return os.path.splitext(filename)[0]

# ============================================================
# DRIVE HELPERS
# ============================================================
def download_file_bytes(drive, file_id):
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return buf.getvalue()

def list_folder_files(drive, folder_id):
    out, page = [], None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields='nextPageToken, files(id,name,mimeType)', pageToken=page).execute()
        out.extend(resp.get('files', []))
        page = resp.get('nextPageToken')
        if not page: break
    return out

def list_subfolders(drive, parent_id):
    out, page = [], None
    while True:
        resp = drive.files().list(
            q=f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields='nextPageToken, files(id,name)', pageToken=page).execute()
        out.extend(resp.get('files', []))
        page = resp.get('nextPageToken')
        if not page: break
    return out

def list_children(drive, parent_id, only_folders=False, only_files=False):
    query = f"'{parent_id}' in parents and trashed = false"
    if only_folders:
        query += " and mimeType = 'application/vnd.google-apps.folder'"
    elif only_files:
        query += " and mimeType != 'application/vnd.google-apps.folder'"
    items, page_token = [], None
    while True:
        resp = drive.files().list(
            q=query, fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items

def ensure_font(drive):
    """Match Results font (Etna) — unchanged."""
    if os.path.exists(_FONT_LOCAL): return _FONT_LOCAL
    files = list_folder_files(drive, ASSETS_FOLDER_ID)
    target = _norm(FONT_NAME)
    f = next((fl for fl in files if _norm(os.path.splitext(fl['name'])[0]) == target), None)
    if not f: return None
    try:
        with open(_FONT_LOCAL, 'wb') as fh:
            fh.write(download_file_bytes(drive, f['id']))
        return _FONT_LOCAL
    except Exception as e:
        print(f"Font download error: {e}")
        return None

def load_font(path, size):
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

def get_background_bytes(drive):
    """Match Results background — unchanged."""
    target = _norm(BACKGROUND_DRIVE_NAME)
    for f in list_folder_files(drive, ASSETS_FOLDER_ID):
        if _norm(os.path.splitext(f['name'])[0]) == target:
            return download_file_bytes(drive, f['id'])
    raise RuntimeError(f"Background '{BACKGROUND_DRIVE_NAME}' not found.")

def find_file_by_stem(drive, parent_folder_id, target_stem):
    """MotM-style fuzzy file finder (used for MotM background + flags)."""
    files = list_children(drive, parent_folder_id, only_files=True)
    if not files:
        return None
    target_norm = normalize_fuzzy(target_stem)
    exact = [f for f in files if normalize_fuzzy(strip_extension(f["name"])) == target_norm]
    if exact:
        return exact[0]
    contains = [
        f for f in files
        if target_norm in normalize_fuzzy(strip_extension(f["name"]))
        or normalize_fuzzy(strip_extension(f["name"])) in target_norm
    ]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        return min(contains, key=lambda f: len(f["name"]))
    best_match, best_ratio = None, 0.0
    for f in files:
        ratio = difflib.SequenceMatcher(
            None, target_norm, normalize_fuzzy(strip_extension(f["name"]))).ratio()
        if ratio > best_ratio:
            best_ratio, best_match = ratio, f
    if best_match and best_ratio >= 0.5:
        return best_match
    return None

def load_image_from_bytes(data, filename="", svg_width=1000):
    if filename.lower().endswith(".svg") and fitz is not None:
        doc = fitz.open(stream=data, filetype="svg")
        page = doc[0]
        zoom = svg_width / page.rect.width
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=True)
        png_bytes = pix.tobytes("png")
        return Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    return Image.open(io.BytesIO(data)).convert("RGBA")

def build_team_league_map(client):
    ws = with_retry(client.open_by_key, FIX_SS_ID).worksheet(INDEX_TAB)
    data = with_retry(ws.get_all_values)
    mapping = {}
    for row in data[1:]:
        if len(row) <= max(IDX_COL_TEAM, IDX_COL_LEAGUE):
            continue
        team = (row[IDX_COL_TEAM] or '').strip()
        league = (row[IDX_COL_LEAGUE] or '').strip()
        if team:
            mapping[team.lower()] = league
    return mapping

# ============================================================
# MATCH RESULTS: LOGO / IMAGE EFFECTS (unchanged)
# ============================================================
def remove_edge_background(img, tol=60):
    img = img.convert('RGBA')
    w, h = img.size
    px = img.load()

    corners = [px[0, 0], px[w-1, 0], px[0, h-1], px[w-1, h-1]]
    bg_r = sum(c[0] for c in corners) // 4
    bg_g = sum(c[1] for c in corners) // 4
    bg_b = sum(c[2] for c in corners) // 4

    mask = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            idx = y * w + x
            c = px[x, y]
            if c[3] == 0:
                mask[idx] = 0
                continue
            dist = abs(c[0] - bg_r) + abs(c[1] - bg_g) + abs(c[2] - bg_b)
            mask[idx] = 0 if dist <= tol else 1

    dq = deque()
    visited = bytearray(w * h)

    for x in range(w):
        for y in (0, h - 1):
            idx = y * w + x
            if mask[idx] == 0 and not visited[idx]:
                dq.append((x, y))
                visited[idx] = 1

    for y in range(h):
        for x in (0, w - 1):
            idx = y * w + x
            if mask[idx] == 0 and not visited[idx]:
                dq.append((x, y))
                visited[idx] = 1

    outside = bytearray(w * h)

    while dq:
        x, y = dq.popleft()
        idx = y * w + x
        outside[idx] = 1

        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                nidx = ny * w + nx
                if not visited[nidx] and mask[nidx] == 0:
                    visited[nidx] = 1
                    dq.append((nx, ny))

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if outside[idx]:
                c = px[x, y]
                px[x, y] = (c[0], c[1], c[2], 0)

    cb = img.getbbox()
    return img.crop(cb) if cb else img

def find_logo_file(logo_files, name):
    def try_match(n):
        target = _norm(n)
        if not target: return None
        for f in logo_files:
            if _norm(os.path.splitext(f['name'])[0]) == target:
                return f
        tset = set(_tokens(n))
        best = None
        for f in logo_files:
            lset = set(_tokens(os.path.splitext(f['name'])[0]))
            if tset == lset or tset <= lset or lset <= tset:
                return f
            overlap = len(tset & lset)
            if overlap and overlap >= max(1, min(len(tset), len(lset))):
                best = f
        return best
    f = try_match(name)
    if f: return f
    toks = _tokens(name)
    if toks and toks[-1] in ('a', 'b', 'c', 'd', 'vet', 'vets'):
        f = try_match(' '.join(toks[:-1]))
        if f: return f
    return None

def dominant_color(img):
    im = img.convert('RGBA').copy()
    im.thumbnail((80, 80))
    pixels = [p for p in list(im.getdata()) if p[3] > 20]
    if not pixels: return (128, 128, 128)
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)
    return (r, g, b)

def make_three_stop_gradient(size, left_rgb, right_rgb):
    w, h = size
    grad = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    px = grad.load()
    for x in range(w):
        t = x / (w - 1) if w > 1 else 0.0
        if t <= 0.5:
            tt = t / 0.5
            a = int(round(255 * (1 - tt)))
            r, g, b = left_rgb
        else:
            tt = (t - 0.5) / 0.5
            a = int(round(255 * tt))
            r, g, b = right_rgb
        for y in range(h):
            px[x, y] = (r, g, b, a)
    return grad

def crop_to_content(img):
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img

def fade_bottom(img, fade_frac=0.25):
    img = img.convert('RGBA')
    w, h = img.size
    fade_h = int(h * fade_frac)
    if fade_h <= 0:
        return img
    alpha = img.split()[3]
    apx = alpha.load()
    for y in range(h - fade_h, h):
        factor = (h - y) / fade_h
        for x in range(w):
            apx[x, y] = int(apx[x, y] * factor)
    img.putalpha(alpha)
    return img

def add_white_glow(img, radius=25, layers=3, expand=140):
    pad = expand
    base = Image.new('RGBA', (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    base.alpha_composite(img, (pad, pad))
    alpha = base.split()[3]
    white = Image.new('RGBA', base.size, (255, 255, 255, 0))
    white.putalpha(alpha)
    glow = Image.new('RGBA', base.size, (255, 255, 255, 0))
    for i in range(layers):
        b = white.filter(ImageFilter.GaussianBlur(radius * (i + 1)))
        glow = Image.alpha_composite(glow, b)
    out = Image.alpha_composite(glow, base)
    content_bbox = (pad, pad, pad + img.width, pad + img.height)
    return out, content_bbox

def harden_alpha(img, threshold=128):
    img = img.convert('RGBA')
    r, g, b, a = img.split()
    a = a.point(lambda v: 255 if v >= threshold else 0)
    img.putalpha(a)
    return img

def resolve_logo_img(drive, logo_files, team_name):
    if _norm(team_name) in GALAKSIA_TEAM_CODES or 'galaksia' in team_name.lower():
        lf = find_logo_file(logo_files, GALAKSIA_LOGO_NAME)
        used_fallback = False
    else:
        lf = find_logo_file(logo_files, clean_team_name(team_name))
        used_fallback = False
        if not lf:
            lf = find_logo_file(logo_files, NO_LOGO_NAME)
            used_fallback = True
    if not lf:
        return None, used_fallback
    raw = download_file_bytes(drive, lf['id'])
    img = Image.open(io.BytesIO(raw)).convert('RGBA')
    return remove_edge_background(img), used_fallback

def fit_logo(img, max_size):
    im = img.copy()
    im.thumbnail((max_size, max_size), Image.LANCZOS)
    return im

# ============================================================
# MATCH RESULTS: PLAYER PHOTO (unchanged — random via Picture column)
# ============================================================
def find_player_folder_id(player_folders, player_name):
    target = _norm(player_name)
    if not target: return None
    def base_norm(name):
        return _norm(re.sub(r'\s*\([^)]*\)\s*$', '', name))
    for f in player_folders:
        if base_norm(f['name']) == target: return f['id']
    for f in player_folders:
        if _norm(f['name']) == target: return f['id']
    for f in player_folders:
        fb = base_norm(f['name'])
        if fb.startswith(target) or target.startswith(fb) or target in fb: return f['id']
    return None

def get_player_photo(drive, player_folders, player_name):
    folder_id = find_player_folder_id(player_folders, player_name)
    if not folder_id: return None
    files = list_folder_files(drive, folder_id)
    candidates = [f for f in files if os.path.splitext(f['name'])[0].strip().lower() == 'front']
    if not candidates:
        return None
    random.shuffle(candidates)
    for choice in candidates:
        try:
            data = download_file_bytes(drive, choice['id'])
            Image.open(io.BytesIO(data)).verify()
            cut = remove(data)
            img = Image.open(io.BytesIO(cut)).convert('RGBA')
            alpha = img.split()[3]
            alpha = alpha.filter(ImageFilter.MinFilter(9))
            img.putalpha(alpha)
            img = crop_to_content(img)
            w, h = img.size
            img = img.crop((0, 0, w, int(h * 5 / 8)))
            img = fade_bottom(img, fade_frac=0.25)
            content_bbox = (0, 0, img.width, img.height)
            img.info['content_bbox'] = content_bbox
            return img
        except Exception as e:
            print(f"  skip photo for {player_name}: {e}")
            continue
    return None

def choose_player_photo(drive, ws, players_played, picture_col_idx, row_num, lookback_games=6, exclude_name=None):
    used = set()
    start = max(2, row_num - lookback_games)
    if start <= row_num - 1:
        rng = ws.get_values(f"{gspread.utils.rowcol_to_a1(start, picture_col_idx+1)}:{gspread.utils.rowcol_to_a1(row_num-1, picture_col_idx+1)}")
        for rr in rng:
            if rr and rr[0]:
                used.add(_norm(rr[0]))

    player_folders = list_subfolders(drive, PLAYER_PHOTOS_ROOT_FOLDER_ID)
    folders_map = {_norm(re.sub(r'\s*\([^)]*\)\s*$', '', f['name'])): f for f in player_folders}

    exclude_norm = _norm(exclude_name) if exclude_name else None
    candidates = [p for p in players_played
                  if _norm(p) and _norm(p) in folders_map and _norm(p) not in used
                  and (exclude_norm is None or _norm(p) != exclude_norm)]
    if not candidates:
        return None, None

    random.shuffle(candidates)
    for player_name in candidates:
        img = get_player_photo(drive, player_folders, player_name)
        if img is not None:
            ws.update_cell(row_num, picture_col_idx + 1, player_name)
            return player_name, img
        print(f"  photo failed for {player_name}, trying next...")
    return None, None

# ============================================================
# MATCH RESULTS: FIXTURES (unchanged)
# ============================================================
def load_fixtures(client):
    ss = with_retry(client.open_by_key, FIX_SS_ID)
    out = []
    for sheet_name in ['League & Cup Fixtures', 'Friendly Fixtures']:
        is_friendly = (sheet_name == 'Friendly Fixtures')
        try:
            rows = with_retry(ss.worksheet(sheet_name).get_all_values)
            for r in rows[1:]:
                if len(r) >= 4 and r[0].strip() and r[2].strip() and r[3].strip():
                    out.append((r[0].strip(), r[2].strip(), r[3].strip(), is_friendly))
        except Exception:
            continue
    return out

def date_to_yyyymmdd(v):
    s = str(v).strip()
    m = re.match(r"^(\d{1,2})[\/\.-](\d{1,2})[\/\.-](\d{2,4})$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return s

def find_fixture(fixtures, team_tab, match_date_yyyymmdd):
    t = team_tab.strip().upper()
    for d, home, away, is_friendly in fixtures:
        if date_to_yyyymmdd(d) == match_date_yyyymmdd and (home.strip().upper() == t or away.strip().upper() == t):
            return home.strip(), away.strip(), is_friendly
    return None, None, False

# ============================================================
# MATCH RESULTS: BUILD IMAGE (unchanged)
# ============================================================
def build_image(bg_bytes, font_path, home_logo, away_logo, home_score, away_score,
                scorers, player_photo, home_team_name, away_team_name, league_logo, label_text, is_friendly):
    base = Image.open(io.BytesIO(bg_bytes)).convert('RGBA')
    if base.size != (CANVAS_W, CANVAS_H):
        base = base.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    if player_photo is not None:
        ph = player_photo.copy()
        cb = ph.info.get('content_bbox', (0, 0, ph.width, ph.height))
        cl, ct, cr, cbot = cb
        content_h = cbot - ct
        target_content_h = PLAYER_BOTTOM_Y - PLAYER_TOP_Y
        scale = target_content_h / content_h
        ph = ph.resize((max(1, int(round(ph.width * scale))),
                        max(1, int(round(ph.height * scale)))), Image.LANCZOS)
        sct = int(round(ct * scale))
        scbot = int(round(cbot * scale))
        scl = int(round(cl * scale))
        scr = int(round(cr * scale))
        content_cx = (scl + scr) / 2
        px = int(round(PLAYER_CENTER_X - content_cx))
        py = PLAYER_TOP_Y - sct
        base.alpha_composite(ph, (px, py))
        player_bottom_canvas = py + ph.height
        bg_fade_h = int(ph.height * PLAYER_FADE_FRAC)
        bg_fade_top = player_bottom_canvas - bg_fade_h
        if bg_fade_h > 1 and bg_fade_top < CANVAS_H:
            overlay = Image.new('RGBA', (CANVAS_W, bg_fade_h), (0, 0, 0, 0))
            opx = overlay.load()
            for yy in range(bg_fade_h):
                a = int(255 * (yy / (bg_fade_h - 1)))
                for xx in range(CANVAS_W):
                    opx[xx, yy] = (0, 0, 0, a)
            base.alpha_composite(overlay, (0, bg_fade_top))

    draw = ImageDraw.Draw(base)

    lc = dominant_color(home_logo) if home_logo else (100, 100, 100)
    rc = dominant_color(away_logo) if away_logo else (100, 100, 100)
    grad = make_three_stop_gradient((CANVAS_W, BAR_BOTTOM - BAR_TOP), lc, rc)
    base.alpha_composite(grad, (0, BAR_TOP))

    if home_logo is not None:
        hl = fit_logo(home_logo, LOGO_MAX)
        base.alpha_composite(hl, (int(HOME_LOGO_CENTER[0] - hl.width/2), int(HOME_LOGO_CENTER[1] - hl.height/2)))
    if away_logo is not None:
        al = fit_logo(away_logo, LOGO_MAX)
        base.alpha_composite(al, (int(AWAY_LOGO_CENTER[0] - al.width/2), int(AWAY_LOGO_CENTER[1] - al.height/2)))

    draw = ImageDraw.Draw(base)

    f_score = load_font(font_path, SCORE_FONT_SIZE)
    score_cx = 384
    score_cy = (BAR_TOP + BAR_BOTTOM) // 2 + 20
    gap = 45
    draw.text((score_cx, score_cy), "-", font=f_score, fill=TEXT_COLOR, anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
    db = draw.textbbox((0, 0), "-", font=f_score)
    dash_half = (db[2] - db[0]) / 2
    draw.text((score_cx - dash_half - gap, score_cy), str(home_score), font=f_score, fill=TEXT_COLOR, anchor="rm", stroke_width=3, stroke_fill=(0, 0, 0))
    draw.text((score_cx + dash_half + gap, score_cy), str(away_score), font=f_score, fill=TEXT_COLOR, anchor="lm", stroke_width=3, stroke_fill=(0, 0, 0))

    def draw_team_name(name, center_x):
        name = name.upper()
        size = AWAY_NAME_SIZE
        f_name = load_font(font_path, size)
        while size > 12:
            f_name = load_font(font_path, size)
            b = draw.textbbox((0, 0), name, font=f_name)
            if (b[2] - b[0]) <= AWAY_NAME_MAX_W:
                break
            size -= 1
        draw.text((center_x, AWAY_NAME_CY), name, font=f_name, fill=TEXT_COLOR, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))

    if home_team_name:
        draw_team_name(home_team_name, HOME_LOGO_CENTER[0])
    if away_team_name:
        draw_team_name(away_team_name, AWAY_LOGO_CENTER[0])

    f_scorer = load_font(font_path, SCORERS_FONT_SIZE)
    y = SCORERS_START_Y
    for minutes, name in scorers:
        if minutes:
            draw.text((SCORERS_MIN_X, y), minutes, font=f_scorer, fill=TEXT_COLOR, anchor="ls", stroke_width=2, stroke_fill=(0, 0, 0))
        draw.text((SCORERS_NAME_X, y), name.upper(), font=f_scorer, fill=TEXT_COLOR, anchor="ls", stroke_width=2, stroke_fill=(0, 0, 0))
        y += SCORERS_LINE_H

    if is_friendly:
        f_fr = load_font(font_path, FRIENDLY_FONT_SIZE)
        draw.text((FRIENDLY_RIGHT_X, FRIENDLY_Y), "FRIENDLY", font=f_fr, fill=TEXT_COLOR, anchor="rs", stroke_width=2, stroke_fill=(0, 0, 0))
        f_label = load_font(font_path, LABEL_FONT_SIZE)
        draw.text((FRIENDLY_RIGHT_X, FRIENDLY_Y + LABEL_FONT_SIZE + 12), label_text.upper(),
                  font=f_label, fill=TEXT_COLOR, anchor="rs", stroke_width=2, stroke_fill=(0, 0, 0))
    else:
        if league_logo is not None:
            ll = fit_logo(league_logo, LEAGUE_LOGO_MAX)
            base.alpha_composite(ll, (int(LEAGUE_LOGO_CENTER[0] - ll.width/2), int(LEAGUE_LOGO_CENTER[1] - ll.height/2)))
        if label_text:
            f_label = load_font(font_path, LABEL_FONT_SIZE)
            draw.text((LEAGUE_LOGO_CENTER[0], LABEL_Y), label_text.upper(), font=f_label, fill=TEXT_COLOR, anchor="mm", stroke_width=2, stroke_fill=(0, 0, 0))

    return base.convert('RGB')

# ============================================================
# MATCH RESULTS: ROW / SCORER EXTRACTION (unchanged, adapted to scan ALL rows)
# ============================================================
def find_rows_missing_post(ws):
    """Returns list of (row_num, headers, post_idx) for every row with empty 'Post'."""
    rows = with_retry(ws.get_all_values)
    if len(rows) < 2:
        return [], [], -1
    headers = [h.strip().lower() for h in rows[0]]
    try:
        post_idx = headers.index('post')
    except ValueError:
        raise RuntimeError(f"Sheet '{ws.title}' missing 'Post' column")
    result = []
    for i, row in enumerate(rows[1:], start=2):
        val = row[post_idx].strip() if len(row) > post_idx and row[post_idx] else ""
        if val == "":
            result.append(i)
    return result, headers, post_idx

def extract_scorers(headers, row_vals):
    order = []
    by_name = {}
    for n in range(1, 16):
        m_col = f"goal {n} - minute"
        s_col = f"goal {n} - scorer"
        if m_col in headers and s_col in headers:
            mi, si = headers.index(m_col), headers.index(s_col)
            minute = row_vals[mi].strip() if len(row_vals) > mi and row_vals[mi] else ""
            scorer = row_vals[si].strip() if len(row_vals) > si and row_vals[si] else ""
            if not scorer:
                continue
            mm = minute.replace("'", "'").strip()
            if mm and not mm.endswith("'"):
                mm += "'"
            key = _norm(scorer)
            if key not in by_name:
                by_name[key] = {'name': scorer, 'mins': []}
                order.append(key)
            if mm:
                by_name[key]['mins'].append(mm)
    out = []
    for key in order:
        entry = by_name[key]
        out.append((' '.join(entry['mins']), entry['name']))
    return out

# ============================================================
# MOTM: NATIONALITY LOOKUP (adapted — uses gspread client like the rest of the script,
# instead of the raw Sheets API service used in the original standalone script)
# ============================================================
def get_nationality_for_player(client, player_name):
    ws = with_retry(client.open_by_key(PERSONAL_INFO_SS_ID).worksheet, PERSONAL_INFO_TAB)
    rows = with_retry(ws.get_all_values)
    if not rows:
        return None
    headers = rows[0]
    try:
        name_idx = headers.index("Name")
        nat_idx = headers.index("Nationality")
    except ValueError as e:
        raise RuntimeError(f"Expected columns not found in Personal Info headers: {e}")

    target_norm = normalize_fuzzy(player_name)
    best_row, best_ratio = None, 0.0
    for row in rows[1:]:
        if len(row) <= name_idx:
            continue
        row_name = row[name_idx]
        row_norm = normalize_fuzzy(row_name)
        if row_norm == target_norm:
            best_row, best_ratio = row, 1.0
            break
        ratio = difflib.SequenceMatcher(None, target_norm, row_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_row = ratio, row

    if best_row is None or best_ratio < 0.5:
        return None
    if len(best_row) <= nat_idx:
        return None
    return best_row[nat_idx].strip() or None

# ============================================================
# MOTM: PLAYER PHOTO FOLDER LOOKUP (unchanged fuzzy logic from standalone script)
# ============================================================
def find_folder_by_fuzzy_name(drive, parent_folder_id, target_name):
    folders = list_children(drive, parent_folder_id, only_folders=True)
    if not folders:
        return None
    target_norm = normalize_fuzzy(target_name)
    target_tokens = set(target_norm.split())

    candidates = []
    for f in folders:
        f_norm = normalize_fuzzy(f["name"])
        f_tokens = set(f_norm.split())
        if target_tokens.issubset(f_tokens):
            candidates.append(f)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return min(candidates, key=lambda f: len(f["name"]))

    best_match, best_ratio = None, 0.0
    for f in folders:
        ratio = difflib.SequenceMatcher(None, target_norm, normalize_fuzzy(f["name"])).ratio()
        if ratio > best_ratio:
            best_ratio, best_match = ratio, f
    if best_match and best_ratio >= 0.5:
        return best_match
    return None

def find_file_by_stem_motm(drive, parent_folder_id, target_stem):
    """Same as find_file_by_stem in Part 2 — kept as separate name to mirror
    original script's naming exactly (behaviorally identical)."""
    return find_file_by_stem(drive, parent_folder_id, target_stem)

# ============================================================
# MOTM: IMAGE COMPOSITION HELPERS (unchanged)
# ============================================================
def remove_background_motm(img):
    data = io.BytesIO()
    img.save(data, format="PNG")
    result_bytes = remove(data.getvalue())
    return Image.open(io.BytesIO(result_bytes)).convert("RGBA")

def crop_to_content_motm(img):
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[3]
    bbox = alpha.getbbox()
    if bbox:
        return img.crop(bbox)
    return img

def crop_top_fraction(img, fraction):
    w, h = img.size
    new_h = max(1, int(h * fraction))
    return img.crop((0, 0, w, new_h))

def fit_font_to_width(draw, text, font_path, max_font_size, target_width):
    font_size = max_font_size
    font = ImageFont.truetype(font_path, font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    if text_w <= target_width:
        return font
    scale = target_width / text_w
    font_size = max(1, int(font_size * scale))
    font = ImageFont.truetype(font_path, font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    while text_w > target_width and font_size > 1:
        font_size -= 1
        font = ImageFont.truetype(font_path, font_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
    return font

def compose_motm_image(background, photo, flag, player_name, output_path):
    bg = background.convert("RGBA")
    canvas_w, canvas_h = bg.size
    canvas = bg.copy()

    flag_w = int(canvas_w * 0.75)
    flag_ratio = flag.height / flag.width
    flag_h = int(flag_w * flag_ratio)
    flag_resized = flag.resize((flag_w, flag_h))
    alpha = flag_resized.split()[3]
    alpha = alpha.point(lambda p: int(p * FLAG_ALPHA_FACTOR))
    flag_resized.putalpha(alpha)
    flag_x = (canvas_w - flag_w) // 2
    flag_y = int(canvas_h * FLAG_TOP_MARGIN_RATIO)
    canvas.alpha_composite(flag_resized, (flag_x, flag_y))

    photo_clean = remove_background_motm(photo)
    photo_clean = crop_to_content_motm(photo_clean)
    photo_clean = crop_top_fraction(photo_clean, PHOTO_TOP_CROP_FRACTION)
    target_photo_h = int(canvas_h * PHOTO_HEIGHT_RATIO)
    photo_ratio = photo_clean.width / photo_clean.height
    target_photo_w = int(target_photo_h * photo_ratio)
    photo_resized = photo_clean.resize((target_photo_w, target_photo_h))
    photo_x = (canvas_w - target_photo_w) // 2
    photo_y = canvas_h - target_photo_h
    canvas.alpha_composite(photo_resized, (photo_x, photo_y))

    draw = ImageDraw.Draw(canvas)
    name_text = player_name.upper()
    target_width = int(canvas_w * NAME_TARGET_WIDTH_RATIO)
    name_font = fit_font_to_width(draw, name_text, MOTM_FONT_LOCAL, NAME_FONT_SIZE_MAX, target_width)

    bbox = draw.textbbox((0, 0), name_text, font=name_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    name_x = (canvas_w - text_w) // 2
    name_y = canvas_h - text_h - 60

    outline_range = 3
    for dx in range(-outline_range, outline_range + 1):
        for dy in range(-outline_range, outline_range + 1):
            if dx != 0 or dy != 0:
                draw.text((name_x + dx, name_y + dy), name_text, font=name_font, fill=(0, 0, 0, 255))
    draw.text((name_x, name_y), name_text, font=name_font, fill=(255, 255, 255, 255))

    canvas.convert("RGB").save(output_path, quality=95)
    print(f"Saved: {output_path}")

def build_motm_image(drive, client, player_name, motm_background, out_path):
    """Adapted main() pipeline from standalone MotM script.
    player_name now comes from the sheet's 'Player of the Match' column."""
    photo_folder = find_folder_by_fuzzy_name(drive, PLAYER_PHOTOS_ROOT_FOLDER_ID, player_name)
    if not photo_folder:
        raise RuntimeError(f"Could not find a photo folder for player '{player_name}'")

    front_file = find_file_by_stem_motm(drive, photo_folder["id"], "front")
    if not front_file:
        raise RuntimeError(f"Could not find a 'front' photo in folder '{photo_folder['name']}'")
    photo_bytes = download_file_bytes(drive, front_file["id"])
    photo_img = load_image_from_bytes(photo_bytes, filename=front_file["name"])

    nationality = get_nationality_for_player(client, player_name)
    if not nationality:
        raise RuntimeError(f"Could not find nationality for player '{player_name}' in sheet")

    flag_file = find_file_by_stem_motm(drive, FLAGS_FOLDER_ID, nationality)
    if not flag_file:
        raise RuntimeError(f"Could not find a flag file for nationality '{nationality}'")
    flag_bytes = download_file_bytes(drive, flag_file["id"])
    flag_img = load_image_from_bytes(flag_bytes, filename=flag_file["name"])

    compose_motm_image(
        background=motm_background,
        photo=photo_img,
        flag=flag_img,
        player_name=player_name,
        output_path=out_path,
    )

# ============================================================
# STORY VERSION GENERATOR (unchanged from lineup script)
# ============================================================
def make_story_version(feed_img_path):
    feed = Image.open(feed_img_path).convert('RGB')
    bg = feed.copy()
    scale = max(STORY_W / bg.width, STORY_H / bg.height)
    bg = bg.resize((int(bg.width * scale), int(bg.height * scale)), Image.LANCZOS)
    left = (bg.width - STORY_W) // 2
    top = (bg.height - STORY_H) // 2
    bg = bg.crop((left, top, left + STORY_W, top + STORY_H))
    bg = bg.filter(ImageFilter.GaussianBlur(40))
    fg = feed.copy()
    fscale = min(STORY_W / fg.width, STORY_H / fg.height) * 0.92
    fg = fg.resize((int(fg.width * fscale), int(fg.height * fscale)), Image.LANCZOS)
    bg.paste(fg, ((STORY_W - fg.width) // 2, (STORY_H - fg.height) // 2))
    out = feed_img_path.replace('.png', '_story.png')
    bg.save(out, 'PNG', quality=95)
    return out

def github_raw_url(local_path):
    rel = local_path.replace('\\', '/')
    return GITHUB_REPO_RAW_BASE + rel

# ============================================================
# META: PAGE TOKEN HELPER (unchanged from lineup script)
# ============================================================
def _get_page_token(page_id, user_token):
    r = requests.get('%s/me/accounts' % GRAPH,
                     params={'access_token': user_token, 'limit': 200})
    r.raise_for_status()
    for p in r.json().get('data', []):
        if str(p.get('id')) == str(page_id):
            return p['access_token']
    raise RuntimeError('Page %s not found in me/accounts' % page_id)

# ============================================================
# META: SINGLE-IMAGE STORY POSTING (unchanged from lineup script)
# ============================================================
def _fb_page_photo(page_id, token, image_url, caption, published=True):
    r = requests.post('%s/%s/photos' % (GRAPH, page_id),
                      data={'url': image_url, 'caption': caption,
                            'published': 'true' if published else 'false',
                            'access_token': token})
    r.raise_for_status()
    return r.json()

def _fb_story(page_id, token, photo_id):
    r = requests.post('%s/%s/photo_stories' % (GRAPH, page_id),
                      data={'photo_id': photo_id, 'access_token': token})
    r.raise_for_status()
    return r.json()

def _ig_publish(ig_id, token, image_url, is_story=True):
    data = {'image_url': image_url, 'access_token': token, 'media_type': 'STORIES'}
    c = requests.post('%s/%s/media' % (GRAPH, ig_id), data=data)
    c.raise_for_status()
    creation_id = c.json()['id']
    for _ in range(10):
        st = requests.get('%s/%s' % (GRAPH, creation_id),
                          params={'fields': 'status_code', 'access_token': token})
        code = st.json().get('status_code')
        if code == 'FINISHED':
            break
        if code == 'ERROR':
            raise RuntimeError('IG container error: %s' % st.text)
        time.sleep(3)
    p = requests.post('%s/%s/media_publish' % (GRAPH, ig_id),
                      data={'creation_id': creation_id, 'access_token': token})
    p.raise_for_status()
    return p.json()

# ============================================================
# META: STORY POST WRAPPER (unchanged from lineup script)
# Returns (fb_ok, ig_ok) instead of raising, so caller can decide overall
# match success (Match Results script's version raised on total failure;
# here we need granular success flags per platform).
# ============================================================
def post_story_to_meta(story_url, caption=''):
    cfg = load_meta_config()
    page_id = cfg['page_id']; ig_id = cfg['ig_user_id']
    user_token = cfg['page_access_token']
    if not story_url:
        raise RuntimeError('no story url; cannot post.')
    try:
        token = _get_page_token(page_id, user_token)
    except Exception as e:
        print('    [meta] could not derive Page token: %s' % e)
        token = user_token

    fb_ok = False
    ig_ok = False

    try:
        photo = _fb_page_photo(page_id, token, story_url, caption, published=False)
        _fb_story(page_id, token, photo['id'])
        print('    [meta] FB story OK')
        fb_ok = True
    except Exception as e:
        print('    [meta] FB story FAILED: %s' % e)

    try:
        _ig_publish(ig_id, user_token, story_url, is_story=True)
        print('    [meta] IG story OK')
        ig_ok = True
    except Exception as e:
        print('    [meta] IG story FAILED: %s' % e)

    return fb_ok, ig_ok

# ============================================================
# META: CAROUSEL POSTING (new — FB multi-photo post + IG carousel)
# ============================================================
def _fb_upload_unpublished_photo(page_id, token, image_url):
    r = requests.post('%s/%s/photos' % (GRAPH, page_id),
                      data={'url': image_url, 'published': 'false',
                            'access_token': token})
    r.raise_for_status()
    return r.json()['id']

def _fb_carousel_post(page_id, token, image_urls, caption=''):
    media_ids = [_fb_upload_unpublished_photo(page_id, token, url) for url in image_urls]
    attached_media = [{'media_fbid': mid} for mid in media_ids]
    r = requests.post('%s/%s/feed' % (GRAPH, page_id),
                      data={'message': caption,
                            'attached_media': json.dumps(attached_media),
                            'access_token': token})
    r.raise_for_status()
    return r.json()

def _ig_carousel_child(ig_id, token, image_url):
    r = requests.post('%s/%s/media' % (GRAPH, ig_id),
                      data={'image_url': image_url,
                            'is_carousel_item': 'true',
                            'access_token': token})
    r.raise_for_status()
    return r.json()['id']

def _ig_carousel_post(ig_id, token, image_urls, caption=''):
    child_ids = [_ig_carousel_child(ig_id, token, url) for url in image_urls]
    c = requests.post('%s/%s/media' % (GRAPH, ig_id),
                      data={'media_type': 'CAROUSEL',
                            'children': ','.join(child_ids),
                            'caption': caption,
                            'access_token': token})
    c.raise_for_status()
    creation_id = c.json()['id']
    for _ in range(10):
        st = requests.get('%s/%s' % (GRAPH, creation_id),
                          params={'fields': 'status_code', 'access_token': token})
        code = st.json().get('status_code')
        if code == 'FINISHED':
            break
        if code == 'ERROR':
            raise RuntimeError('IG carousel container error: %s' % st.text)
        time.sleep(3)
    p = requests.post('%s/%s/media_publish' % (GRAPH, ig_id),
                      data={'creation_id': creation_id, 'access_token': token})
    p.raise_for_status()
    return p.json()

# ============================================================
# META: CAROUSEL POST WRAPPER (mirrors post_story_to_meta)
# ============================================================
def post_carousel_to_meta(image_urls, caption=''):
    cfg = load_meta_config()
    page_id = cfg['page_id']; ig_id = cfg['ig_user_id']
    user_token = cfg['page_access_token']
    if not image_urls:
        raise RuntimeError('no image urls; cannot post carousel.')
    try:
        token = _get_page_token(page_id, user_token)
    except Exception as e:
        print('    [meta] could not derive Page token: %s' % e)
        token = user_token

    fb_ok = False
    ig_ok = False

    try:
        _fb_carousel_post(page_id, token, image_urls, caption)
        print('    [meta] FB carousel OK')
        fb_ok = True
    except Exception as e:
        print('    [meta] FB carousel FAILED: %s' % e)

    try:
        _ig_carousel_post(ig_id, user_token, image_urls, caption)
        print('    [meta] IG carousel OK')
        ig_ok = True
    except Exception as e:
        print('    [meta] IG carousel FAILED: %s' % e)

    return fb_ok, ig_ok

# ============================================================
# MAIN RUN (generate + post, split via --generate-only / --post-only)
# ============================================================
def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    errors = []

    print('Auth...'); sys.stdout.flush()
    client = get_gspread_client()
    drive = get_drive_service()
    print('Auth OK'); sys.stdout.flush()

    resp_ss = with_retry(client.open_by_key, RESP_SS_ID)

    # ---- Step 1: collect every pending row (Post empty) across all tabs ----
    pending = []  # list of dicts, one per match-row
    for tab in RESP_TABS:
        ws = with_retry(resp_ss.worksheet, tab)
        row_nums, headers, post_idx = find_rows_missing_post(ws)
        if not row_nums:
            continue
        for row_num in row_nums:
            pending.append({
                'tab': tab,
                'ws': ws,
                'row_num': row_num,
                'headers': headers,
                'post_idx': post_idx,
            })

    if not pending:
        print('No rows with empty Post in any 6-a-side tab.')
        return

    print('Found %d pending match row(s) to process.' % len(pending))

    if GENERATE_ONLY:
        print('Loading background & font...'); sys.stdout.flush()
        bg_bytes = get_background_bytes(drive)
        font_path = ensure_font(drive)

        motm_bg_file = find_file_by_stem(drive, ASSETS_FOLDER_ID, MOTM_BACKGROUND_FILE_STEM)
        if not motm_bg_file:
            raise RuntimeError(f"MotM background '{MOTM_BACKGROUND_FILE_STEM}' not found.")
        motm_bg_bytes = download_file_bytes(drive, motm_bg_file['id'])
        motm_background = load_image_from_bytes(motm_bg_bytes, filename=motm_bg_file['name'])

        logo_files = list_folder_files(drive, LOGOS_FOLDER_ID)
        fixtures = load_fixtures(client)
        team_league = build_team_league_map(client)

        print('Assets loaded.'); sys.stdout.flush()

        match_groups = []  # one entry per match, in original pending order

        for item in pending:
            tab = item['tab']; ws = item['ws']; row_num = item['row_num']
            headers = item['headers']; post_idx = item['post_idx']

            row_vals = with_retry(ws.row_values, row_num)

            def hidx(name):
                n = name.lower()
                if n not in headers:
                    raise RuntimeError(f"Tab '{tab}' missing column '{name}'")
                return headers.index(n)

            try:
                pic_idx = hidx("picture")
            except RuntimeError as e:
                errors.append(str(e)); continue

            match_date_val = row_vals[hidx("match date")] if len(row_vals) > hidx("match date") else ""
            match_date_key = date_to_yyyymmdd(match_date_val)

            home_team, away_team, is_friendly = find_fixture(fixtures, tab, match_date_key)
            if not home_team or not away_team:
                errors.append(f"No fixture found for '{tab}' row {row_num} on '{match_date_key}'")
                continue

            home_logo, home_fallback = resolve_logo_img(drive, logo_files, home_team)
            away_logo, away_fallback = resolve_logo_img(drive, logo_files, away_team)

            gal_goals = row_vals[hidx("galaksia goals count")] if len(row_vals) > hidx("galaksia goals count") else "0"
            opp_goals = row_vals[hidx("opponent goals count")] if len(row_vals) > hidx("opponent goals count") else "0"
            try:
                gal_i = int(float(str(gal_goals).replace(",", ".").strip() or "0"))
            except ValueError:
                gal_i = 0
            try:
                opp_i = int(float(str(opp_goals).replace(",", ".").strip() or "0"))
            except ValueError:
                opp_i = 0

            tab_is_home = home_team.strip().upper() == tab.strip().upper()
            home_score = gal_i if tab_is_home else opp_i
            away_score = opp_i if tab_is_home else gal_i

            home_display = home_team if home_fallback else None
            away_display = away_team if away_fallback else None

            scorers = extract_scorers(headers, row_vals)

            players_played = [p.strip() for p in (row_vals[hidx("players who played")] if len(row_vals) > hidx("players who played") else "").split(',') if p.strip()]
            try:
                motm_col_idx_early = hidx("player of the match")
                motm_name_early = (row_vals[motm_col_idx_early].strip()
                                    if len(row_vals) > motm_col_idx_early and row_vals[motm_col_idx_early] else "")
            except RuntimeError:
                motm_name_early = ""
            
            photo_player_name, photo_img = choose_player_photo(
                drive, ws, players_played, pic_idx, row_num, exclude_name=motm_name_early)
            if not photo_player_name:
                errors.append(f"No eligible player photo found for '{tab}' row {row_num}")
                continue

            league = team_league.get(tab.lower(), '')
            league_logo = None
            if league and not is_friendly:
                lf = find_logo_file(logo_files, league)
                if lf:
                    raw = download_file_bytes(drive, lf['id'])
                    league_logo = remove_edge_background(Image.open(io.BytesIO(raw)).convert('RGBA'))

            label_text = f"GP23 {tab}"

            score_line = f"{home_team} {home_score} - {away_score} {away_team}"
            match_type_line = "Friendly" if is_friendly else (league or "League/Cup")
            caption = f"{score_line}\n{match_type_line}"

            mr_img = build_image(
                bg_bytes=bg_bytes, font_path=font_path,
                home_logo=home_logo, away_logo=away_logo,
                home_score=home_score, away_score=away_score,
                scorers=scorers, player_photo=photo_img,
                home_team_name=home_display, away_team_name=away_display,
                league_logo=league_logo, label_text=label_text, is_friendly=is_friendly
            )

            safe_tab = re.sub(r'[^A-Za-z0-9]+', '_', tab)
            date_str = match_date_key.replace('-', '')
            mr_path = os.path.join(OUTPUT_DIR, f'matchresults_{safe_tab}_{date_str}_{row_num}.png')
            mr_img.save(mr_path, 'PNG')
            mr_story_path = make_story_version(mr_path)
            print(f'{tab} row {row_num}: saved {mr_path}')

            # ---- Loss check: no MotM if Opponent Goals > Galaksia Goals ----
            is_loss = opp_i > gal_i
            motm_path = None
            motm_story_path = None
            motm_player_name = None

            if not is_loss:
                try:
                    motm_col_idx = hidx("player of the match")
                except RuntimeError as e:
                    errors.append(str(e))
                    motm_col_idx = None

                if motm_col_idx is not None:
                    motm_player_name = (row_vals[motm_col_idx].strip()
                                         if len(row_vals) > motm_col_idx and row_vals[motm_col_idx] else "")
                    if not motm_player_name:
                        errors.append(f"'{tab}' row {row_num}: not a loss but 'Player of the Match' is empty.")
                    else:
                        motm_path = os.path.join(OUTPUT_DIR, f'motm_{safe_tab}_{date_str}_{row_num}.png')
                        try:
                            build_motm_image(drive, client, motm_player_name, motm_background, motm_path)
                            motm_story_path = make_story_version(motm_path)
                            print(f'{tab} row {row_num}: saved {motm_path}')
                        except Exception as e:
                            errors.append(f"'{tab}' row {row_num}: MotM build failed: {e}")
                            motm_path = None
                            motm_story_path = None

            match_groups.append({
                'tab': tab, 'ws': ws, 'row_num': row_num, 'post_idx': post_idx,
                'mr_path': mr_path, 'mr_story_path': mr_story_path,
                'motm_path': motm_path, 'motm_story_path': motm_story_path,
                'motm_player_name': motm_player_name,
                'is_loss': is_loss,
            })

        send_error_email(errors)
        send_error_email(errors)
        print('Generation complete. %d match(es) ready.' % len(match_groups))
        return

    import glob

    if POST_ONLY and not GENERATE_ONLY:
        print('Post-only: rediscovering pending rows and matching saved files...')
        match_groups = []

        for item in pending:
            tab = item['tab']; ws = item['ws']; row_num = item['row_num']
            headers = item['headers']; post_idx = item['post_idx']
            row_vals = with_retry(ws.row_values, row_num)

            def hidx(name):
                n = name.lower()
                if n not in headers:
                    return None
                return headers.index(n)

            gi = hidx("galaksia goals count"); oi = hidx("opponent goals count")
            try:
                gal_i = int(float(str(row_vals[gi]).replace(",", ".").strip() or "0")) if gi is not None and len(row_vals) > gi else 0
            except ValueError:
                gal_i = 0
            try:
                opp_i = int(float(str(row_vals[oi]).replace(",", ".").strip() or "0")) if oi is not None and len(row_vals) > oi else 0
            except ValueError:
                opp_i = 0
            is_loss = opp_i > gal_i

            safe_tab = re.sub(r'[^A-Za-z0-9]+', '_', tab)

            mr_matches = glob.glob(os.path.join(OUTPUT_DIR, f'matchresults_{safe_tab}_*_{row_num}.png'))
            mr_matches = [p for p in mr_matches if not p.endswith('_story.png')]
            mr_path = mr_matches[0] if mr_matches else None
            mr_story_path = mr_path.replace('.png', '_story.png') if mr_path else None
            if mr_story_path and not os.path.exists(mr_story_path):
                mr_story_path = None

            motm_path = None
            motm_story_path = None
            motm_player_name = None
            if not is_loss:
                pm_idx = hidx("player of the match")
                motm_player_name = (row_vals[pm_idx].strip()
                                     if pm_idx is not None and len(row_vals) > pm_idx and row_vals[pm_idx] else "")
                mm_matches = glob.glob(os.path.join(OUTPUT_DIR, f'motm_{safe_tab}_*_{row_num}.png'))
                mm_matches = [p for p in mm_matches if not p.endswith('_story.png')]
                motm_path = mm_matches[0] if mm_matches else None
                motm_story_path = motm_path.replace('.png', '_story.png') if motm_path else None
                if motm_story_path and not os.path.exists(motm_story_path):
                    motm_story_path = None

            if not mr_path:
                print(f'{tab} row {row_num}: no Match Results image found on disk - skipping.')
                continue

            match_groups.append({
                'tab': tab, 'ws': ws, 'row_num': row_num, 'post_idx': post_idx,
                'mr_path': mr_path, 'mr_story_path': mr_story_path,
                'motm_path': motm_path, 'motm_story_path': motm_story_path,
                'motm_player_name': motm_player_name, 'is_loss': is_loss,
                'caption': f'{tab} Match Results',
            })

        for m in match_groups:
            tab = m['tab']; ws = m['ws']; row_num = m['row_num']; post_idx = m['post_idx']
            print(f'--- Posting {tab} row {row_num} ---')

            # ---- Build ordered URL lists ----
            # Reel (carousel): Match Results first, then MotM (if present)
            reel_urls = [github_raw_url(m['mr_path'])]
            if m['motm_path']:
                reel_urls.append(github_raw_url(m['motm_path']))

            caption = m.get('caption', f'{tab} Match Results')
            if m['motm_path']:
                caption += f'\n\nPlayer of the Match: {m["motm_player_name"]}'

            reel_fb_ok, reel_ig_ok = post_carousel_to_meta(reel_urls, caption=caption)

            # ---- Stories: Match Results first, then MotM after (separate posts) ----
            mr_story_url = github_raw_url(m['mr_story_path']) if m['mr_story_path'] else None
            story1_fb_ok, story1_ig_ok = (False, False)
            if mr_story_url:
                story1_fb_ok, story1_ig_ok = post_story_to_meta(mr_story_url)
            else:
                print(f'{tab} row {row_num}: no Match Results story image - skipping story post.')

            story2_fb_ok, story2_ig_ok = (True, True)  # default true if no MotM (nothing required)
            if m['motm_path']:
                motm_story_url = github_raw_url(m['motm_story_path']) if m['motm_story_path'] else None
                if motm_story_url:
                    story2_fb_ok, story2_ig_ok = post_story_to_meta(motm_story_url)
                else:
                    print(f'{tab} row {row_num}: no MotM story image - skipping story post.')
                    story2_fb_ok, story2_ig_ok = (False, False)

            # ---- Success = ALL required posts succeeded on BOTH platforms ----
            all_fb_ok = reel_fb_ok and story1_fb_ok and story2_fb_ok
            all_ig_ok = reel_ig_ok and story1_ig_ok and story2_ig_ok
            fully_sent = all_fb_ok and all_ig_ok

            if fully_sent:
                with_retry(ws.update_cell, row_num, post_idx + 1, 'Sent')
                if m['motm_path'] and m['motm_player_name']:
                    # Re-fetch headers fresh (match_groups doesn't carry them)
                    header_row = with_retry(ws.row_values, 1)
                    header_row = with_retry(ws.row_values, 1)
                    headers_lower = [h.strip().lower() for h in header_row]
                    try:
                        pm_col_idx = headers_lower.index('motm')
                    except ValueError:
                        pm_col_idx = None
                    if pm_col_idx is not None:
                        with_retry(ws.update_cell, row_num, pm_col_idx + 1, m['motm_player_name'])
                        print(f'{tab} row {row_num}: Picture MotM = {m["motm_player_name"]}')
                    else:
                        print(f'{tab} row {row_num}: WARNING - "Picture MotM" column not found.')
                print(f'{tab} row {row_num}: marked Sent.')
            else:
                print(f'{tab} row {row_num}: NOT marked Sent '
                      f'(FB ok={all_fb_ok}, IG ok={all_ig_ok}).')

            # ---- Cleanup local files regardless of success (per your instruction) ----
            for p in (m['mr_path'], m['mr_story_path'], m['motm_path'], m['motm_story_path']):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception as e:
                        print(f'  [cleanup] could not remove {p}: {e}')

        print('Posting complete.')


# ============================================================
# ENTRYPOINT
# ============================================================
GENERATE_ONLY = '--generate-only' in sys.argv
POST_ONLY = '--post-only' in sys.argv
if not GENERATE_ONLY and not POST_ONLY:
    print('ERROR: pass either --generate-only or --post-only.')
    sys.exit(1)

if __name__ == '__main__':
    run()
