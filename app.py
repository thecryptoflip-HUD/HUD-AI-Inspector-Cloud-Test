"""HUD AI Inspector Milestones 1–10 — cloud TEST deployment (synthetic data only)."""
import base64
import zipfile
from collections import Counter
import hmac
import hashlib
from urllib.parse import quote
import streamlit.components.v1 as components
import io
import json
import os
import re
import tempfile
import uuid
import time
import threading
from datetime import datetime, date
from html import escape
from pathlib import Path

import streamlit as st
from PIL import Image, ImageOps
from dotenv import load_dotenv
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as PDFImage, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter

load_dotenv()
ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('HUD_DATA_DIR', '/tmp/hud_inspector_demo_data'))
DATA.mkdir(parents=True, exist_ok=True)
MEDIA = DATA / 'media'
MEDIA.mkdir(exist_ok=True)

st.set_page_config(page_title='HUD AI Inspector | Milestone 10', page_icon='🏠', layout='wide')
st.markdown('''<style>.block-container{max-width:1100px;padding-top:1.3rem}button{min-height:2.5rem} @media(max-width:650px){.block-container{padding-left:1rem;padding-right:1rem}}</style>''', unsafe_allow_html=True)
st.markdown("""
<style>
html, body {
    overscroll-behavior-y: none;
}
[data-testid="stAppViewContainer"] {
    overscroll-behavior-y: contain;
}
</style>
""", unsafe_allow_html=True)

from supabase import create_client

BUCKET = "inspection-evidence"

def config(name, default=""):
    value = os.getenv(name, "")
    if value:
        return value
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

def require_login():
    """Username UI; Supabase Auth email/password behind a server-side mapping.
    This test-only mapping must be kept in private app secrets, never Git.
    """
    url = config("SUPABASE_URL")
    key = config("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        st.error("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in private Streamlit secrets.")
        st.stop()
    if st.session_state.get("auth_user") and st.session_state.get("sb_client"):
        try:
            client = st.session_state["sb_client"]
            session = client.auth.get_session()
            if session and session.user and session.user.id == st.session_state["auth_user"]:
                return
        except Exception:
            pass
        st.session_state.pop("auth_user", None)
        st.session_state.pop("sb_client", None)
    st.title("🔒 HUD AI Inspector — Sign in")
    with st.form("login"):
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Password", type="password")
        clicked = st.form_submit_button("Sign in", use_container_width=True)
    if clicked:
        try:
            mapping = json.loads(config("HUD_USERNAME_EMAIL_MAP", "{}"))
            email = mapping.get(username)
            if not email:
                raise ValueError("Invalid credentials")
            client = create_client(url, key)
            result = client.auth.sign_in_with_password({"email": email, "password": password})
            if not result.user or not result.session:
                raise ValueError("Invalid credentials")
            profile = client.table("inspector_profiles").select("username").eq("user_id", result.user.id).single().execute().data
            if not profile or profile.get("username") != username:
                raise ValueError("Invalid credentials")
            st.session_state["sb_client"] = client
            st.session_state["auth_user"] = result.user.id
            st.session_state["authenticated"] = True
            st.rerun()
        except Exception:
            st.error("Sign in failed. Check your username and password.")
    st.stop()

require_login()

def client():
    c = st.session_state.get("sb_client")
    if not c or not st.session_state.get("auth_user"):
        st.error("Your session has expired. Sign in again.")
        st.stop()
    return c

def owned_inspection(inspection_id):
    r = client().table("inspections").select("id").eq("id", inspection_id).limit(1).execute().data
    return bool(r)

def rows(sql, args=()):
    """Narrow, fail-closed translator for the original app's known SELECT statements."""
    s = " ".join(sql.strip().lower().split())
    if s == "select * from inspections order by created_at desc":
        return client().table("inspections").select("*").order("created_at", desc=True).execute().data
    if s == "select id from evidence":
        return client().table("evidence").select("id").execute().data
    if s == "select * from inspections where id=?":
        return client().table("inspections").select("*").eq("id", args[0]).execute().data
    if s.startswith("select * from evidence where inspection_id=?"):
        q = client().table("evidence").select("*").eq("inspection_id", args[0])
        if "order by captured_at,id" in s:
            q = q.order("captured_at").order("id")
        return [decode_evidence(x) for x in q.execute().data]
    if s == "select occurred_at,event from audit_events where inspection_id=? order by occurred_at desc limit 500":
        return client().table("audit_events").select("occurred_at,event").eq("inspection_id",args[0]).order("occurred_at",desc=True).limit(500).execute().data
    raise RuntimeError("Unsupported database query: " + sql)

def decode_evidence(record):
    item = dict(record)
    for field, default in (("findings_json", []), ("reviewed_json", {})):
        value = item.get(field)
        item[field] = json.dumps(value if value is not None else default)
    return item

def execute(sql, args):
    s = " ".join(sql.strip().lower().split())
    if s == "insert into inspections values (?,?,?,?,?,?,?,?)":
        keys = ("id","property","address","unit","inspector","inspected_on","notes","created_at")
        record = dict(zip(keys, args))
        record["owner_id"] = st.session_state["auth_user"]
        client().table("inspections").insert(record).execute()
        return
    if s == "insert into evidence values (?,?,?,?,?,?,?,?,?,?)":
        keys = ("id","inspection_id","area","room","kind","path","source_name","captured_at","findings_json","reviewed_json")
        record = dict(zip(keys, args))
        for field in ("findings_json", "reviewed_json"):
            record[field] = json.loads(record[field])
        client().table("evidence").insert(record).execute()
        return
    if s.startswith("update evidence set ") and " where id=?" in s:
        if s == "update evidence set findings_json=? where id=?":
            values = {"findings_json": json.loads(args[0])}
        elif s == "update evidence set findings_json=?,reviewed_json=? where id=?":
            values = {"findings_json": json.loads(args[0]), "reviewed_json": json.loads(args[1])}
        elif s == "update evidence set reviewed_json=? where id=?":
            values = {"reviewed_json": json.loads(args[0])}
        else:
            raise RuntimeError("Unsupported evidence update")
        evidence_id = args[-1]
        changed = client().table("evidence").update(values).eq("id", evidence_id).execute().data
        if not changed:
            raise PermissionError("Evidence was not found or is not owned by this inspector")
        if "reviewed_json" in values:
            # The database trigger creates an audit event, without review text.
            pass
        return
    raise RuntimeError("Unsupported database mutation: " + sql)

def media_path(item):
    """Fetch an authorized object into a session/user-scoped local render cache."""
    name = Path(item["path"]).name
    if name != item["path"] or not re.fullmatch(r"[0-9a-f-]{36}\.jpg", name):
        raise ValueError("Invalid media name")
    iid = item["inspection_id"]
    if not owned_inspection(iid):
        raise PermissionError("Inspection not accessible")
    folder = MEDIA / st.session_state["auth_user"] / iid
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / name
    if not destination.exists():
        raw = client().storage.from_(BUCKET).download(f"{st.session_state['auth_user']}/{iid}/{name}")
        destination.write_bytes(raw)
    return destination

def save_photo(inspection_id, area, room, img, source, kind="photo"):
    if not owned_inspection(inspection_id):
        raise PermissionError("Inspection not accessible")
    eid = str(uuid.uuid4())
    name = eid + ".jpg"
    raw = image_bytes(img)
    object_path = f"{st.session_state['auth_user']}/{inspection_id}/{name}"
    client().storage.from_(BUCKET).upload(object_path, raw, {"content-type": "image/jpeg", "upsert": "false"})
    try:
        execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid,inspection_id,area,room,kind,name,source,datetime.now().isoformat(timespec="seconds"),"[]","{}"))
    except Exception:
        try:
            client().storage.from_(BUCKET).remove([object_path])
        except Exception:
            pass
        raise
    folder = MEDIA / st.session_state["auth_user"] / inspection_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(raw)
    return eid

def restore_inspection_backup(uploaded_file):
    """Development safety: block restore until atomic import has been validated."""
    raise RuntimeError("Cloud ZIP restore is intentionally disabled in this development build. Existing ZIP exports remain available. A transaction-safe, verified importer must be tested before enabling restore.")

def current():
    return st.session_state.get('inspection_id')


def image_bytes(img):
    out = io.BytesIO()
    img.convert('RGB').save(out, format='JPEG', quality=83, optimize=True)
    return out.getvalue()


def normalized_image(raw):
    with Image.open(io.BytesIO(raw)) as src:
        img = ImageOps.exif_transpose(src)
        img.thumbnail((1600, 1600))
        return img.convert('RGB').copy()


def analyze(img, area, room):
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key:
        raise RuntimeError('OPENAI_API_KEY is missing from the app secrets.')
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=90)
    payload = base64.b64encode(image_bytes(img)).decode('ascii')
    prompt = f'''You assist a human property inspector. Location supplied by inspector: {area} / {room}.
Identify ONLY clearly visible potential maintenance/safety conditions. Do not invent measurements, test results, NSPIRE standards, official deficiency levels, or compliance decisions. When uncertain, say so. Do not claim the entire room was inspected. Return at most 8 potential observations. If nothing evident, return an empty findings list. Never assign official HUD/NSPIRE citations or severity.'''
    schema = {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string'},
            'findings': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'visual_evidence': {'type': 'string'},
                        'field_check': {'type': 'string'},
                        'confidence': {'type': 'string', 'enum': ['low', 'medium', 'high']}
                    },
                    'required': ['title', 'visual_evidence', 'field_check', 'confidence'],
                    'additionalProperties': False
                }
            }
        },
        'required': ['summary', 'findings'],
        'additionalProperties': False
    }
    resp = client.responses.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
        input=[{'role': 'user', 'content': [
            {'type': 'input_text', 'text': prompt},
            {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + payload}
        ]}],
        text={'format': {'type': 'json_schema', 'name': 'inspection_screening', 'strict': True, 'schema': schema}},
        max_output_tokens=2200
    )
    if resp.status != 'completed':
        raise RuntimeError('AI response was incomplete. Please retry the analysis.')
    output = (resp.output_text or '').strip()
    if not output:
        raise RuntimeError('AI returned no findings text. Please retry the analysis.')
    parsed = json.loads(output)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('findings'), list):
        raise ValueError('AI response did not contain the expected findings structure.')
    return {'summary': str(parsed.get('summary', '')),
            'findings': [x for x in parsed['findings'] if isinstance(x, dict)][:8]}


def frames_from_video(raw, suffix, max_frames=8):
    try:
        import cv2
    except ImportError as e:
        raise RuntimeError('Video processing needs opencv-python-headless installed.') from e
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise ValueError('Cannot decode this video. Try an MP4 (H.264) recording.')
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        if count <= 0:
            raise ValueError('Video has no readable frames.')
        indexes = sorted(set(int(i*(count-1)/max(1,max_frames-1)) for i in range(min(max_frames,count))))
        result = []
        for frame_index in indexes:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if ok:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame)
                img.thumbnail((1600,1600))
                result.append((round(frame_index/fps, 1), img.copy()))
        if not result:
            raise ValueError('Could not extract video frames.')
        return result
    finally:
        if cap:
            cap.release()
        os.unlink(path)


def finding_summary(evidence):
    counts=Counter()
    total=0
    for item in evidence:
        try:
            findings=json.loads(item.get('findings_json') or '[]')
            reviews=json.loads(item.get('reviewed_json') or '{}')
        except (ValueError,TypeError):
            continue
        for idx, finding in enumerate(findings):
            total+=1
            determination=str(reviews.get(str(idx),{}).get('determination') or 'Not reviewed')
            counts[determination]+=1
    return total,counts


def inspector_summary_details(evidence):
    """Derive a read-only inspector summary from saved evidence and reviews."""
    details = []
    for item in evidence:
        try:
            findings = json.loads(item.get('findings_json') or '[]')
            reviews = json.loads(item.get('reviewed_json') or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(findings, list) or not isinstance(reviews, dict):
            continue
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                continue
            review = reviews.get(str(index), {})
            if not isinstance(review, dict):
                review = {}
            details.append({
                'location': ' / '.join(part for part in [item.get('area'), item.get('room')] if part) or 'Unspecified',
                'title': str(finding.get('title') or 'Potential observation'),
                'status': str(review.get('determination') or 'Not reviewed'),
                'notes': str(review.get('notes') or ''),
                'reference': str(review.get('reference') or ''),
                'field_check': str(finding.get('field_check') or ''),
                'source': str(item.get('source_name') or ''),
            })
    return details




def inspection_quality_checks(inspection, evidence):
    """Read-only completeness checks; NOT HUD/NSPIRE compliance testing."""
    checks = []
    def add(category, status, detail):
        checks.append({'category': category, 'status': status, 'detail': detail})

    for label, key in [('Property / building', 'property'), ('Inspector name', 'inspector'),
                       ('Inspection date', 'inspected_on')]:
        if not str(inspection.get(key) or '').strip():
            add('Inspection details', 'Needs attention', f'{label} is missing.')
    if not str(inspection.get('unit') or '').strip():
        add('Inspection details', 'Review', 'Unit is blank; confirm whether a unit applies.')
    if not evidence:
        add('Evidence', 'Needs attention', 'No evidence images or frames are saved.')
    for item in evidence:
        name = Path(item.get('path') or '').name
        if not name or not media_path(item).is_file():
            add('Evidence', 'Needs attention', f'Missing evidence file for record {item.get("id", "unknown")}.')
        try:
            findings = json.loads(item.get('findings_json') or '[]')
            reviews = json.loads(item.get('reviewed_json') or '{}')
            if not isinstance(findings, list) or not isinstance(reviews, dict):
                raise ValueError('Incorrect finding/review structure')
        except (ValueError, TypeError):
            add('Observation records', 'Needs attention', f'Unreadable review data in evidence {item.get("id", "unknown")}.')
            continue
        for idx, finding in enumerate(findings):
            if not isinstance(finding, dict):
                add('Observation records', 'Needs attention', f'Invalid observation {idx + 1} in evidence {item.get("id", "unknown")}.')
                continue
            review = reviews.get(str(idx), {})
            if not isinstance(review, dict):
                review = {}
            title = str(finding.get('title') or f'Observation {idx + 1}')
            determination = str(review.get('determination') or 'Not reviewed')
            if determination in ('Not reviewed', 'Needs closer inspection'):
                add('Inspector review', 'Needs attention', f'{title}: {determination}.')
            elif not str(review.get('notes') or '').strip():
                add('Inspector documentation', 'Review', f'{title}: final decision has no inspector notes.')
            if determination == 'Confirmed observation' and not str(review.get('reference') or '').strip():
                add('Reference verification', 'Review', f'{title}: confirmed observation has no inspector-entered NSPIRE reference.')
    if not checks:
        add('Completeness', 'No issues flagged', 'No missing fields or pending reviews detected by these limited checks.')
    return checks


def quality_checks_csv(inspection, checks):
    import csv
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Inspection ID', 'Property', 'Category', 'Status', 'Detail'])
    for check in checks:
        writer.writerow([inspection.get('id', ''), inspection.get('property', ''),
                         check['category'], check['status'], check['detail']])
    return buffer.getvalue().encode('utf-8-sig')



def validate_backup_read_only(upload):
    """Read-only bounded structural validation; does not import or trust backup content."""
    import stat
    if upload.size > 100 * 1024 * 1024:
        raise ValueError('Backup exceeds 100 MB limit.')
    with zipfile.ZipFile(io.BytesIO(upload.getvalue())) as z:
        members = z.infolist()
        if len(members) > 500 or sum(m.file_size for m in members) > 250 * 1024 * 1024:
            raise ValueError('Archive exceeds entry count or uncompressed size limits.')
        names = [m.filename for m in members]
        if len(names) != len(set(names)) or 'inspection.json' not in names:
            raise ValueError('Duplicate paths or missing inspection.json.')
        for m in members:
            name = m.filename
            if m.flag_bits & 1 or stat.S_ISLNK(m.external_attr >> 16):
                raise ValueError('Encrypted or symlink entries are not supported.')
            if name != 'inspection.json' and (not name.startswith('media/') or len(name.split('/')) != 2 or '..' in name or '\\' in name or m.is_dir()):
                raise ValueError('Unexpected media path in archive.')
            if m.file_size > (5 if name == 'inspection.json' else 25) * 1024 * 1024:
                raise ValueError('Oversized archive member.')
        meta = json.loads(z.read('inspection.json'))
        if meta.get('format') != 'HUD_AI_INSPECTOR_M4_PORTABLE_BACKUP':
            raise ValueError('Unsupported backup format.')
        if not isinstance(meta.get('inspection'), dict) or not isinstance(meta.get('evidence'), list):
            raise ValueError('Invalid backup metadata.')
        if len(meta['evidence']) > 499:
            raise ValueError('Too many evidence records.')
        for m in members:
            with z.open(m) as f:
                while f.read(1024 * 1024):
                    pass  # Read every entry to detect corrupted CRC data.
        return {'media_files': sum(n.startswith('media/') for n in names),
                'evidence_records': len(meta['evidence']), 'property': str(meta['inspection'].get('property', ''))[:100]}


def test_plan_csv():
    import csv
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(['Milestone', 'Test', 'Expected result', 'Pass / Fail', 'Notes'])
    for row in [
        (1, 'Create and reopen synthetic inspection', 'Inspection reopens with correct metadata'),
        (2, 'Upload synthetic photo', 'Evidence image appears'),
        (3, 'Optional AI screening', 'Suggestions appear or helpful configuration warning'),
        (4, 'Camera preview and sampled scanner', 'Camera permissions and frames work on supported browser'),
        (5, 'Review and report', 'Decisions saved; PDF and ZIP downloadable'),
        (6, 'Quality Check', 'Outstanding review and evidence gaps flagged'),
        (7, 'Audit Trail', 'Review save creates dated event'),
        (8, 'Backup Validator', 'Exported ZIP validates without restoring'),
        (9, 'Security Readiness', 'Security limitations displayed accurately'),
        (10, 'End-to-end test', 'Export, restore and compare with synthetic test data')]:
        w.writerow([*row, '', ''])
    return b.getvalue().encode('utf-8-sig')

def full_inspection_backup(inspection,evidence):
    """Portable ZIP with metadata and images, no passwords/API keys."""
    output=io.BytesIO()
    metadata={'format':'HUD_AI_INSPECTOR_M4_PORTABLE_BACKUP','inspection':inspection,'evidence':evidence}
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('inspection.json',json.dumps(metadata,indent=2))
        for item in evidence:
            name=Path(item.get('path') or '').name
            file=media_path(item)
            if name and file.is_file():
                archive.write(file,arcname='media/'+name)
    return output.getvalue()


def pdf_report(inspection, evidence):
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)
    styles = getSampleStyleSheet()
    story = [Paragraph('HUD AI Inspector — Inspector Review Report', styles['Title']), Spacer(1,12)]
    for key, label in [('property','Property'),('address','Address'),('unit','Unit'),('inspector','Inspector'),('inspected_on','Inspection date')]:
        story.append(Paragraph(f'<b>{label}:</b> {escape(str(inspection.get(key) or "Not entered"))}', styles['BodyText']))
        story.append(Spacer(1,5))
    story.append(Spacer(1,10))
    story.append(Paragraph('<b>Important:</b> AI findings are visual screening suggestions only. This is not an official HUD inspection, NSPIRE deficiency classification, or compliance determination. Physical tests and inspector verification are required.', styles['BodyText']))
    total, counts = finding_summary(evidence)
    story.extend([Spacer(1,18),Paragraph('Inspection summary',styles['Heading2']),
                  Paragraph(f'Evidence images / frames: {len(evidence)}',styles['BodyText']),
                  Paragraph(f'AI-suggested potential observations: {total}',styles['BodyText'])])
    for status, number in sorted(counts.items()):
        story.append(Paragraph(f'{escape(status)}: {number}',styles['BodyText']))
    story.append(Spacer(1,8))
    story.append(Paragraph('Unreviewed observations require inspector verification; an absence of AI observations is not an inspection pass.',styles['BodyText']))
    if inspection.get('notes'):
        story.extend([Spacer(1,12), Paragraph('General inspector notes', styles['Heading2']),
                      Paragraph(escape(str(inspection['notes'])).replace('\n', '<br/>'), styles['BodyText'])])
    summary_details = inspector_summary_details(evidence)
    follow_up = [d for d in summary_details if d['status'] in ('Not reviewed', 'Needs closer inspection')]
    story.extend([Spacer(1,14), Paragraph('Inspector review and follow-up summary', styles['Heading2'])])
    if not summary_details:
        story.append(Paragraph('No AI-suggested observations recorded. This is not an inspection pass.', styles['BodyText']))
    else:
        story.append(Paragraph(f'Observations requiring review or closer inspection: {len(follow_up)}', styles['BodyText']))
        for d in summary_details:
            story.append(Paragraph(
                f'<b>{escape(d["status"])}:</b> {escape(d["title"])} — {escape(d["location"])}', styles['BodyText']))
            if d['notes']:
                story.append(Paragraph(f'Inspector notes: {escape(d["notes"])}', styles['BodyText']))
            if d['reference']:
                story.append(Paragraph(f'Inspector-entered NSPIRE reference (verify): {escape(d["reference"])}', styles['BodyText']))
            story.append(Spacer(1,5))
    for idx, item in enumerate(evidence,1):
        story.extend([PageBreak(), Paragraph(f'Evidence {idx}: {escape(item["area"] or "Unspecified")} / {escape(item["room"] or "Unspecified")}', styles['Heading2'])])
        if item['kind']=='video_frame':
            story.append(Paragraph(f'Video frame: {escape(item["source_name"] or "")}', styles['BodyText']))
        image_path = media_path(item)
        if image_path.is_file():
            with Image.open(image_path) as img:
                width,height=img.size
            scale=min(480/width,300/height,1)
            story.append(PDFImage(str(image_path),width=width*scale,height=height*scale))
            story.append(Spacer(1,12))
        parsed=json.loads(item['findings_json'] or '[]')
        reviews=json.loads(item['reviewed_json'] or '{}')
        if not parsed:
            story.append(Paragraph('No AI findings recorded. This does not mean the location passed inspection.', styles['BodyText']))
        for j, finding in enumerate(parsed):
            review=reviews.get(str(j),{})
            for label, value in [('Potential observation',finding.get('title','')),('Visible evidence',finding.get('visual_evidence','')),('Field check',finding.get('field_check','')),('Inspector determination',review.get('determination','Not reviewed')),('Inspector notes',review.get('notes','')),('NSPIRE reference (inspector-entered)',review.get('reference','Not verified'))]:
                story.append(Paragraph(f'<b>{escape(label)}:</b> {escape(str(value))}', styles['BodyText']))
                story.append(Spacer(1,5))
            story.append(Spacer(1,10))
    doc.build(story)
    return out.getvalue()


def speech_to_text(upload):
    key=os.getenv('OPENAI_API_KEY','').strip()
    if not key:
        raise RuntimeError('OPENAI_API_KEY is required for speech transcription.')
    from openai import OpenAI
    client=OpenAI(api_key=key,timeout=90)
    data=io.BytesIO(upload.getvalue())
    data.name='inspection_question.wav'
    response=client.audio.transcriptions.create(model=os.getenv('OPENAI_TRANSCRIBE_MODEL','gpt-4o-mini-transcribe'),file=data)
    return response.text.strip()


def speak_text(text):
    """User-initiated browser speech; does not send speech to a remote server."""
    import json as _json
    phrase=_json.dumps(text[:3500]).replace('</','<\\/')
    components.html(f"""<button style='font-size:16px;padding:12px' onclick='speechSynthesis.cancel();speechSynthesis.speak(new SpeechSynthesisUtterance({phrase}))'>🔊 Read answer aloud</button><button style='font-size:16px;padding:12px' onclick='speechSynthesis.cancel()'>Stop</button>""",height=62)


def reference_matches(query, limit=6):
    import pypdf
    pdf_dir=DATA/'nspire_references'
    pdf_dir.mkdir(exist_ok=True)
    terms=[t.lower() for t in re.findall(r'[a-zA-Z0-9]+',query) if len(t)>2]
    results=[]
    for path in sorted(pdf_dir.glob('*.pdf')):
        try:
            reader=pypdf.PdfReader(str(path))
            for idx,page in enumerate(reader.pages):
                content=page.extract_text() or ''
                low=content.lower()
                score=sum(low.count(term) for term in terms)
                if score:
                    match_positions=[low.find(term) for term in terms if term in low]
                    pos=min(match_positions) if match_positions else 0
                    start=max(0,pos-170)
                    results.append({'file':path.name,'page':idx+1,'excerpt':content[start:start+900].replace('\n',' '),'score':score})
        except Exception:
            continue
    return sorted(results,key=lambda x:x['score'],reverse=True)[:limit]




# -------------------------------------------------------------
# MILESTONE 3: LIVE FRAME SAMPLING (WebRTC + explicit AI review)
# -------------------------------------------------------------
class LiveFrameBuffer:
    """Thread-safe latest-frame buffer; never stores the full video stream."""
    def __init__(self):
        self.lock = threading.Lock()
        self.latest_jpeg = None
        self.latest_at = 0.0
        self.captured = 0

    def accept(self, frame):
        # WebRTC callback runs on a worker thread. Do NOT call Streamlit or AI here.
        now = time.monotonic()
        with self.lock:
            if now - self.latest_at < 1.0:
                return
        try:
            import cv2
            arr = frame.to_ndarray(format='bgr24')
            height, width = arr.shape[:2]
            ratio = min(1.0, 960.0 / max(height, width))
            if ratio < 1.0:
                arr = cv2.resize(arr, (int(width*ratio), int(height*ratio)))
            ok, encoded = cv2.imencode('.jpg', arr, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            if not ok:
                return
            payload = encoded.tobytes()
        except Exception:
            return
        with self.lock:
            self.latest_jpeg = payload
            self.latest_at = now
            self.captured += 1

    def snapshot(self):
        with self.lock:
            return self.latest_jpeg, self.latest_at, self.captured

@st.cache_resource(show_spinner=False)
def get_live_buffer(session_token):
    return LiveFrameBuffer()


def review_live_frame(frame_bytes, inspection_id, area, room, run_ai):
    """Persist sampled evidence; optional AI screening, never official compliance."""
    image = normalized_image(frame_bytes)
    eid = save_photo(inspection_id, area, room, image, 'Live WebRTC sampled frame', 'live_frame')
    if run_ai:
        try:
            result = analyze(image, area, room)
            execute('UPDATE evidence SET findings_json=? WHERE id=?',
                    (json.dumps(result['findings']), eid))
            return eid, result['summary'], len(result['findings'])
        except Exception as exc:
            return eid, f'Frame saved; AI analysis failed: {exc}', 0
    return eid, 'Frame saved for inspector review.', 0


st.title('🏠 HUD AI Inspector')
st.caption('Milestones 1–10 • CLOUD TEST • Phone camera • AI screening • Voice • PDF summary • Supabase development storage')
st.error('CLOUD TEST ONLY: Use invented property details and synthetic/non-resident photos. Cloud storage is under development and is not approved for real resident records. Do not upload real resident data or private inspection evidence.')
st.warning('AI observations are not official NSPIRE findings. Confirm relevant HUD standards, perform required physical checks, and document your own determination.')

with st.sidebar:
    st.header('Inspection workspace')
    page = st.radio('Go to', ['Dashboard', 'New / Open Inspection', 'Capture & Upload', 'Live Camera Preview', 'Continuous Live Scanner', 'Review Findings', 'Milestone 6 Review Center', 'Milestone 6 Quality Check', 'Milestone 7 Audit Trail', 'Milestone 8 Backup Validator', 'Milestone 9 Security Readiness', 'Milestone 10 Test Center', 'NSPIRE Smart Assistant', 'NSPIRE Reference Search', 'Report & Export', 'Phone Setup'], label_visibility='collapsed')
    st.caption('Supabase test-account authentication is enabled. Synthetic data only.')
    if st.button('Sign out'):
        st.session_state['authenticated'] = False
        try:
            client().auth.sign_out()
        except Exception:
            pass
        st.session_state.pop('sb_client', None)
        st.session_state.pop('auth_user', None)
        st.session_state.pop('inspection_id', None)
        st.rerun()

if page=='Dashboard':
    items=rows('SELECT * FROM inspections ORDER BY created_at DESC')
    c1,c2=st.columns(2)
    c1.metric('Saved inspections',len(items))
    c2.metric('Evidence images / frames',len(rows('SELECT id FROM evidence')))
    st.subheader('Inspection history')
    if items:
        st.dataframe([{'Property':x['property'],'Unit':x['unit'],'Date':x['inspected_on'],'Inspector':x['inspector'],'ID':x['id']} for x in items],hide_index=True,use_container_width=True)
    else:
        st.info('Create an inspection to begin.')

elif page=='New / Open Inspection':
    st.subheader('Create inspection')
    with st.form('new'):
        property_name=st.text_input('Property / building *')
        address=st.text_input('Property address')
        unit=st.text_input('Unit')
        inspector=st.text_input('Inspector')
        day=st.date_input('Inspection date',date.today())
        notes=st.text_area('General notes')
        submitted=st.form_submit_button('Create inspection',use_container_width=True)
    if submitted:
        if not property_name.strip():
            st.error('Property / building is required.')
        else:
            iid=str(uuid.uuid4())
            execute('INSERT INTO inspections VALUES (?,?,?,?,?,?,?,?)',(iid,property_name.strip(),address,unit,inspector,day.isoformat(),notes,datetime.now().isoformat(timespec='seconds')))
            st.session_state['inspection_id']=iid
            st.success('Inspection created. Open Capture & Upload to add evidence.')
    items=rows('SELECT * FROM inspections ORDER BY created_at DESC')
    if items:
        choices={f"{x['property']} | Unit {x['unit']} | {x['inspected_on']} | {x['id'][:8]}":x['id'] for x in items}
        default=list(choices.values()).index(current()) if current() in choices.values() else 0
        choice=st.selectbox('Open existing inspection',list(choices),index=default)
        if st.button('Open selected inspection',use_container_width=True):
            st.session_state['inspection_id']=choices[choice]
            st.success('Inspection opened.')

    st.divider()
    st.subheader('Restore Inspection Backup (ZIP)')

    backup_file = st.file_uploader(
        'Choose a previously downloaded inspection backup ZIP',
        type=['zip'],
        key='restore_backup'
    )

    if backup_file and st.button('Restore inspection backup'):
        try:
            restored_id, photo_count = restore_inspection_backup(backup_file)

            st.session_state['inspection_id'] = restored_id

            st.success(
                f'Inspection restored successfully with '
                f'{photo_count} evidence images.'
            )

            st.info(
                'Your restored inspection is now selected. '
                'Open Review Findings to check the results.'
            )

        except Exception as error:
            st.error(f'Restore failed: {error}')

elif page=='Capture & Upload':
    if not current():
        st.info('Create or open an inspection first in New / Open Inspection.')
    else:
        st.subheader('Add inspection evidence')
        area=st.selectbox('Inspectable area',['Unit','Inside','Outside'])
        room=st.text_input('Room / location',placeholder='Bathroom, kitchen, hallway, exterior...')
        method=st.radio('Evidence type',['Photo upload','Phone camera photo','Recorded video upload'],horizontal=True)
        if method=='Photo upload':
            uploaded=st.file_uploader('Upload photos',type=['jpg','jpeg','png','webp'],accept_multiple_files=True)
            if uploaded and st.button('Save uploaded photos'):
                for f in uploaded:
                    try:
                        save_photo(current(),area,room,normalized_image(f.getvalue()),f.name)
                    except Exception as e:
                        st.error(f'Unable to save {f.name}: {e}')
                st.success('Photo import finished.')
        elif method=='Phone camera photo':
            camera=st.camera_input('Take a photo')
            if camera and st.button('Save camera photo'):
                save_photo(current(),area,room,normalized_image(camera.getvalue()),'Phone camera')
                st.success('Photo saved.')
        else:
            st.caption('Video is sampled into up to 8 still frames; this is not continuous live scanning. MP4/MOV decoding depends on installed codecs.')
            video=st.file_uploader('Upload recorded video',type=['mp4','mov','avi','m4v'])
            if video and st.button('Extract and save video frames'):
                if video.size > 150*1024*1024:
                    st.error('Video exceeds 150 MB. Record a shorter clip.')
                else:
                    try:
                        extracted=frames_from_video(video.getvalue(),Path(video.name).suffix.lower())
                        for timestamp,img in extracted:
                            save_photo(current(),area,room,img,f'{video.name} @ {timestamp}s','video_frame')
                        st.success(f'Saved {len(extracted)} sample frames. Open Review Findings to analyze them.')
                    except Exception as e:
                        st.error(str(e))
        st.caption('For accurate review, include close-ups and perform any necessary physical tests yourself.')

elif page=='Live Camera Preview':
    st.subheader('📹 Live phone camera — preview')
    st.info('The live preview below runs in your browser. It does not stream video to AI or save frames. Use the camera capture below to save an inspection photo for analysis. On phones, HTTPS is generally required for camera access.')
    components.html("""<div style='font:16px sans-serif'><video id='v' autoplay playsinline muted style='width:100%;max-height:340px;background:#222;border-radius:12px'></video><p id='status'>Tap Start camera to allow access.</p><button id='start' style='padding:12px'>Start camera preview</button><button id='stop' style='padding:12px'>Stop</button></div><script>let stream;const v=document.getElementById('v');document.getElementById('start').onclick=async()=>{try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});v.srcObject=stream;document.getElementById('status').textContent='Live preview active (not being recorded).'}catch(e){document.getElementById('status').textContent='Camera unavailable: '+e.message}};document.getElementById('stop').onclick=()=>{if(stream)stream.getTracks().forEach(t=>t.stop());v.srcObject=null;document.getElementById('status').textContent='Stopped.'};</script>""",height=470)
    if not current():
        st.warning('Open an inspection in New / Open Inspection before saving photos.')
    else:
        area=st.selectbox('Inspectable area',['Unit','Inside','Outside'],key='live_area')
        room=st.text_input('Room / location',key='live_room')
        photo=st.camera_input('Capture a still photo for AI review',key='live_photo')
        if photo and st.button('Save captured photo',use_container_width=True):
            try:
                save_photo(current(),area,room,normalized_image(photo.getvalue()),'Live camera still')
                st.success('Photo saved. Open Review Findings to analyze it.')
            except Exception as e:
                st.error(f'Could not save photo: {e}')


elif page=='Continuous Live Scanner':
    st.subheader('📹 Continuous Live AI Scanner — sampled frames')
    st.warning('This is continuous camera preview with periodic STILL-FRAME analysis, not real-time analysis of every video frame. AI observations are preliminary and may miss hazards. Physically verify all findings.')
    st.caption('Your browser must permit camera access. Phones generally require HTTPS. No microphone audio is requested.')
    if not current():
        st.info('First create or open an inspection in New / Open Inspection.')
    else:
        try:
            from streamlit_webrtc import webrtc_streamer, WebRtcMode
        except ImportError:
            st.error('Missing dependency: install requirements.txt again to add streamlit-webrtc.')
            st.stop()
        area=st.selectbox('Inspectable area',['Unit','Inside','Outside'],key='continuous_area')
        room=st.text_input('Room / location',value='Living room',key='continuous_room')
        interval=st.select_slider('Seconds between saved/checked frames',options=[5,10,15,20,30,60],value=15)
        max_frames=st.number_input('Maximum frames per scanning session',min_value=1,max_value=100,value=12,step=1)
        ai_enabled=st.checkbox('Analyze sampled frames with AI (API charges apply)',value=False)
        st.caption('Every sampled frame is saved as inspection evidence. AI analysis sends that frame to your configured OpenAI API. Avoid recording people or private resident information.')
        if 'live_session_token' not in st.session_state:
            st.session_state['live_session_token']=str(uuid.uuid4())
        buffer=get_live_buffer(st.session_state['live_session_token'])
        if 'live_scanned' not in st.session_state:
            st.session_state['live_scanned']=0
        if 'live_last_checked' not in st.session_state:
            st.session_state['live_last_checked']=0.0
        if 'live_last_frame_time' not in st.session_state:
            st.session_state['live_last_frame_time']=0.0
        if st.button('Reset session counters (does not delete saved evidence)'):
            st.session_state['live_scanned']=0
            st.session_state['live_last_checked']=0.0
            st.session_state['live_last_frame_time']=0.0
        rtc=webrtc_streamer(
            key='hud-continuous-webrtc',
            mode=WebRtcMode.SENDONLY,
            video_frame_callback=lambda frame: (buffer.accept(frame) or frame),
            media_stream_constraints={'video':{'facingMode':{'ideal':'environment'}},'audio':False},
            async_processing=True,
            rtc_configuration={'iceServers':[{'urls':['stun:stun.l.google.com:19302']}]},
        )
        st.caption('Press START above to begin; STOP ends camera streaming. Keep this page open during scanning.')
        @st.fragment(run_every=2)
        def live_scan_poll():
            active=bool(rtc.state.playing)
            count=st.session_state['live_scanned']
            st.metric('Frames saved in this session',count)
            if not active:
                st.info('Camera stopped. Start the stream to sample frames.')
                return
            if count>=max_frames:
                st.warning('Frame limit reached. Press STOP above; reset counters to scan again.')
                return
            data, timestamp, received=buffer.snapshot()
            if not data:
                st.info('Waiting for camera frames…')
                return
            if timestamp <= st.session_state['live_last_frame_time']:
                st.caption('Waiting for a fresh camera frame…')
                return
            now=time.monotonic()
            if now-st.session_state['live_last_checked'] < interval:
                st.caption(f'Next sampling opportunity in about {int(interval-(now-st.session_state["live_last_checked"]))} seconds.')
                return
            st.session_state['live_last_checked']=now
            st.session_state['live_last_frame_time']=timestamp
            with st.spinner('Saving frame' + (' and screening with AI…' if ai_enabled else '…')):
                try:
                    eid, summary, findings=review_live_frame(data,current(),area,room,ai_enabled)
                    st.session_state['live_scanned']+=1
                    st.success(f'Frame {st.session_state["live_scanned"]} saved. Potential observations: {findings}.')
                    st.write(summary)
                    st.image(data,caption='Most recently saved frame',use_container_width=True)
                except Exception as exc:
                    st.error(f'Frame processing failed: {exc}')
        live_scan_poll()
        st.info('After scanning, open Review Findings to verify each observation and add notes. The PDF report includes saved frames and inspector decisions.')

elif page=='Review Findings':
    if not current():
        st.info('Open an inspection first.')
    else:
        evidence=rows('SELECT * FROM evidence WHERE inspection_id=? ORDER BY captured_at,id',(current(),))
        st.subheader(f'Review evidence ({len(evidence)})')
        if not evidence:
            st.info('No evidence saved yet.')
        for i,item in enumerate(evidence):
            with st.expander(f'{i+1}. {item["area"]} / {item["room"]} — {item["source_name"]}',expanded=i==0):
                path=media_path(item)
                if not path.exists():
                    st.error('Evidence image missing from local storage.')
                    continue
                st.image(str(path),use_container_width=True)
                if st.button('Analyze this image with AI',key=f'ai_{item["id"]}'):
                    try:
                        with st.spinner('Analyzing visible conditions...'):
                            with Image.open(path) as img:
                                result=analyze(img,item['area'],item['room'])
                        execute('UPDATE evidence SET findings_json=?,reviewed_json=? WHERE id=?',(json.dumps(result['findings']), '{}',item['id']))
                        st.success(result['summary'] or 'Analysis saved.')
                        st.rerun()
                    except Exception as e:
                        st.error(f'AI analysis failed: {e}')
                findings=json.loads(item['findings_json'] or '[]')
                reviews=json.loads(item['reviewed_json'] or '{}')
                if not findings:
                    st.caption('No AI findings saved for this image. This is not an inspection pass.')
                for j,finding in enumerate(findings):
                    st.markdown(f'**Potential observation {j+1}: {finding.get("title","Condition")}**')
                    st.write('Visible evidence:',finding.get('visual_evidence',''))
                    st.write('Verify in person:',finding.get('field_check',''))
                    saved=reviews.get(str(j),{})
                    with st.form(f'review_{item["id"]}_{j}'):
                        options=['Not reviewed','Confirmed observation','Rejected AI observation','Needs closer inspection','No deficiency observed']
                        determination=st.selectbox('Inspector determination',options,index=options.index(saved.get('determination','Not reviewed')) if saved.get('determination') in options else 0)
                        note=st.text_area('Inspector notes',value=saved.get('notes',''))
                        reference=st.text_input('Verified official NSPIRE standard/reference (optional)',value=saved.get('reference',''))
                        if st.form_submit_button('Save inspector review'):
                            reviews[str(j)]={'determination':determination,'notes':note,'reference':reference}
                            execute('UPDATE evidence SET reviewed_json=? WHERE id=?',(json.dumps(reviews),item['id']))
                            st.success('Inspector review saved.')


elif page=='Milestone 6 Review Center':
    st.subheader('🧭 Milestone 6 — Inspection Review Center')
    st.caption('Organize AI-suggested observations, document inspector decisions, and export a follow-up checklist. Priority cues are for workflow only, not NSPIRE deficiency or severity classifications.')
    if not current():
        st.info('Open a test inspection in New / Open Inspection first.')
    else:
        inspection_rows=rows('SELECT * FROM inspections WHERE id=?',(current(),))
        evidence=rows('SELECT * FROM evidence WHERE inspection_id=? ORDER BY captured_at,id',(current(),))
        entries=[]
        for ev in evidence:
            try:
                findings=json.loads(ev.get('findings_json') or '[]')
                reviews=json.loads(ev.get('reviewed_json') or '{}')
            except (ValueError,TypeError):
                st.warning('One evidence record contains unreadable observation data; skipped.')
                continue
            if not isinstance(findings,list) or not isinstance(reviews,dict):
                continue
            for idx,finding in enumerate(findings):
                if not isinstance(finding,dict):
                    continue
                rev=reviews.get(str(idx),{})
                if not isinstance(rev,dict): rev={}
                entries.append({'evidence_id':ev['id'],'index':idx,'area':ev.get('area') or '',
                    'room':ev.get('room') or '', 'source':ev.get('source_name') or '',
                    'title':str(finding.get('title') or 'Potential observation'),
                    'visual_evidence':str(finding.get('visual_evidence') or ''),
                    'field_check':str(finding.get('field_check') or ''),
                    'determination':str(rev.get('determination') or 'Not reviewed'),
                    'notes':str(rev.get('notes') or ''),'reference':str(rev.get('reference') or ''),
                    'reviews':reviews})
        pending=[e for e in entries if e['determination'] in ('Not reviewed','Needs closer inspection')]
        confirmed=[e for e in entries if e['determination']=='Confirmed observation']
        resolved=[e for e in entries if e['determination'] in ('Rejected AI observation','No deficiency observed')]
        a,b,c,d=st.columns(4)
        a.metric('Potential observations',len(entries))
        b.metric('Follow-up pending',len(pending))
        c.metric('Inspector confirmed',len(confirmed))
        d.metric('Rejected / no deficiency',len(resolved))
        if not entries:
            st.info('No saved AI observations yet. Add synthetic evidence in Capture & Upload, then review it.')
        else:
            st.progress((len(entries)-len(pending))/len(entries),text=f'{len(entries)-len(pending)} of {len(entries)} observations have a completed review decision')
            st.markdown('#### Filter and review observations')
            f1,f2=st.columns(2)
            statuses=['Not reviewed','Needs closer inspection','Confirmed observation','Rejected AI observation','No deficiency observed']
            chosen=f1.multiselect('Inspector determination',statuses,default=['Not reviewed','Needs closer inspection'])
            term=f2.text_input('Search title, location, evidence, or notes').strip().lower()
            filtered=[e for e in entries if (not chosen or e['determination'] in chosen) and
                      (not term or term in ' '.join(str(e.get(k,'')) for k in ('title','area','room','visual_evidence','notes')).lower())]
            st.caption(f'{len(filtered)} observation(s) match the filters.')
            for e in filtered:
                key=f"m5_{e['evidence_id']}_{e['index']}"
                with st.expander(f"{e['title']} — {e['area']} / {e['room']} ({e['determination']})"):
                    st.write('**AI visual observation:**',e['visual_evidence'] or 'Not supplied')
                    st.write('**Suggested on-site check:**',e['field_check'] or 'Not supplied')
                    st.caption('Source: '+e['source'])
                    with st.form(key):
                        choice=st.selectbox('Inspector determination',statuses,index=statuses.index(e['determination']) if e['determination'] in statuses else 0)
                        notes=st.text_area('Inspector notes',value=e['notes'])
                        reference=st.text_input('Official NSPIRE reference verified by inspector (optional)',value=e['reference'])
                        if st.form_submit_button('Save this review'):
                            updated=dict(e['reviews'])
                            updated[str(e['index'])]={'determination':choice,'notes':notes,'reference':reference}
                            execute('UPDATE evidence SET reviewed_json=? WHERE id=?',(json.dumps(updated),e['evidence_id']))
                            st.success('Review saved.')
                            st.rerun()
            st.divider()
            st.markdown('#### Follow-up checklist')
            if pending:
                for e in pending:
                    st.checkbox(f"{e['title']} — {e['area']} / {e['room']} ({e['determination']})",key=f"check_{e['evidence_id']}_{e['index']}",help='Temporary checklist only. Save the official inspector determination in the review form above.')
            else:
                st.success('No AI-suggested observations are currently awaiting a review decision. This does not establish NSPIRE compliance.')
            import csv
            csv_buffer=io.StringIO()
            writer=csv.writer(csv_buffer)
            writer.writerow(['Property','Unit','Location','Potential observation','AI visual evidence','On-site verification','Inspector determination','Inspector notes','Inspector-verified reference','Source'])
            prop=inspection_rows[0].get('property','') if inspection_rows else ''
            unit=inspection_rows[0].get('unit','') if inspection_rows else ''
            for e in entries:
                writer.writerow([prop,unit,' / '.join((e['area'],e['room'])),e['title'],e['visual_evidence'],e['field_check'],e['determination'],e['notes'],e['reference'],e['source']])
            st.download_button('Download complete review checklist (CSV)',csv_buffer.getvalue().encode('utf-8-sig'),file_name='HUD_AI_Inspector_M5_Review_Checklist.csv',mime='text/csv',use_container_width=True)
            st.info('The CSV is a working review checklist, not an official HUD inspection report. Use Report & Export for the existing PDF and complete ZIP backup.')

elif page=='Milestone 6 Quality Check':
    st.subheader('🧾 Milestone 6 — Inspection Quality Check Center')
    st.caption('A read-only completeness check for the saved inspection. This is not a HUD/NSPIRE compliance determination, official inspection, or severity assessment.')
    if not current():
        st.info('Open a test inspection in New / Open Inspection first.')
    else:
        selected=rows('SELECT * FROM inspections WHERE id=?',(current(),))
        if not selected:
            st.warning('Selected inspection was not found. Reopen an inspection.')
        else:
            inspection=selected[0]
            evidence=rows('SELECT * FROM evidence WHERE inspection_id=? ORDER BY captured_at,id',(current(),))
            checks=inspection_quality_checks(inspection,evidence)
            urgent=sum(c['status']=='Needs attention' for c in checks)
            review=sum(c['status']=='Review' for c in checks)
            a,b,c=st.columns(3)
            a.metric('Needs attention',urgent)
            b.metric('Review recommended',review)
            c.metric('Evidence records',len(evidence))
            if urgent:
                st.warning('This inspection has incomplete records or outstanding inspector review decisions.')
            else:
                st.info('No blocking completeness issues detected by these limited checks. This does not establish HUD/NSPIRE compliance.')
            st.markdown('#### Quality check results')
            for item in checks:
                with st.expander(f"{item['status']} — {item['category']}"):
                    st.write(item['detail'])
            st.download_button('Download quality check checklist (CSV)',
                               quality_checks_csv(inspection,checks),
                               file_name='HUD_AI_Inspector_M6_Quality_Check.csv',
                               mime='text/csv',use_container_width=True)
            st.caption('Use Milestone 5 Review Center to finish reviews, and Report & Export for PDF and full ZIP backups. These checks do not replace physical inspection or review of official standards.')


elif page=='Milestone 7 Audit Trail':
    st.subheader('📋 Milestone 7 — Inspector Review Audit Trail')
    st.caption('Records new review-save events in this installation. This is a basic activity log, not a tamper-proof or legally certified audit trail. Older actions are not backfilled.')
    if not current():
        st.info('Open a synthetic inspection first.')
    else:
        events=rows('SELECT occurred_at,event FROM audit_events WHERE inspection_id=? ORDER BY occurred_at DESC LIMIT 500',(current(),))
        st.metric('Recorded review events',len(events))
        if events:
            st.dataframe(events,use_container_width=True)
            import csv
            b=io.StringIO(); w=csv.writer(b); w.writerow(['UTC time','Event'])
            for e in events: w.writerow([e['occurred_at'],e['event']])
            st.download_button('Download activity log (CSV)',b.getvalue().encode('utf-8-sig'),file_name='M7_Review_Audit_Log.csv',mime='text/csv')
        else:
            st.info('No review-save events recorded yet. Save a review to test this feature.')

elif page=='Milestone 8 Backup Validator':
    st.subheader('🗃️ Milestone 8 — Backup Integrity Validator')
    st.caption('Checks the structure and ZIP integrity of a downloaded inspection backup without importing or modifying any inspection.')
    uploaded_backup=st.file_uploader('Choose a synthetic inspection ZIP backup',type=['zip'],key='m8_backup')
    if uploaded_backup and st.button('Validate backup without restoring'):
        try:
            result=validate_backup_read_only(uploaded_backup)
            st.success('ZIP structure and member CRC checks passed. This does not guarantee a successful restore or data completeness.')
            st.write('Evidence records:',result['evidence_records'])
            st.write('Media files:',result['media_files'])
            st.write('Inspection property:',result['property'])
        except Exception as exc:
            st.error(f'Backup validation failed: {exc}')

elif page=='Milestone 9 Security Readiness':
    st.subheader('🔐 Milestone 9 — Security and Deployment Readiness')
    st.error('NOT PRODUCTION READY: free cloud deployment and shared-password access are not appropriate for resident/private inspection evidence.')
    st.caption('This page is a planning checklist, not a security certification or penetration test.')
    requirements=[
        'Private deployment with authenticated individual inspector accounts and authorization',
        'Encryption in transit and encrypted durable storage with managed keys',
        'Managed database and secure, verified off-site backups with tested restores',
        'Data retention and deletion rules, access logging, and incident response plan',
        'Privacy/legal review and appropriate agreements before processing resident data',
        'Validated NSPIRE publication versions and human inspector sign-off',
        'Mobile browser camera, accessibility, and offline-connectivity testing',
    ]
    for idx,requirement in enumerate(requirements):
        st.checkbox(requirement,key=f'm9_plan_{idx}',help='Planning acknowledgement only; checking this does not implement the control.')
    st.warning('Checking items here does not change application security. Continue using invented details and synthetic/non-resident photos only.')

elif page=='Milestone 10 Test Center':
    st.subheader('🧪 Milestone 10 — End-to-End Test Center')
    st.caption('Download a structured manual acceptance test plan for Milestones 1–10. This page does not claim the tests have passed.')
    st.download_button('Download full test plan (CSV)',test_plan_csv(),file_name='HUD_AI_Inspector_M1_to_M10_Test_Plan.csv',mime='text/csv',use_container_width=True)
    st.markdown('#### Current inspection preflight')
    if not current():
        st.info('Open or restore a synthetic inspection to run preflight checks.')
    else:
        found=rows('SELECT * FROM inspections WHERE id=?',(current(),))
        if found:
            ev=rows('SELECT * FROM evidence WHERE inspection_id=?',(current(),))
            checks=inspection_quality_checks(found[0],ev)
            st.metric('Evidence records',len(ev))
            st.metric('Attention flags',sum(x['status']=='Needs attention' for x in checks))
            st.metric('Review suggestions',sum(x['status']=='Review' for x in checks))
            for check in checks: st.write(f"{check['status']}: {check['detail']}")
    st.info('Recommended manual flow: create → capture → review → quality check → PDF/ZIP export → ZIP validate → restore → compare. AI results require inspector verification.')

elif page=='NSPIRE Smart Assistant':
    st.subheader('🎙️ NSPIRE Smart Assistant')
    st.caption('Ask by typing or record a short voice question. Responses are based on the official PDF documents you load into the reference library; always verify edition, applicability, and physical tests.')
    spoken=st.toggle('Offer spoken answers',value=False,key='voice_answers')
    typed=st.text_input('Type an NSPIRE question',placeholder='What does the standard say about smoke alarms?',key='smart_question')
    audio=st.audio_input('Or record your question with your microphone')
    if audio and st.button('Transcribe recorded question'):
        try:
            with st.spinner('Transcribing...'):
                st.session_state['transcribed_question']=speech_to_text(audio)
        except Exception as e:
            st.error(str(e))
    transcribed=st.session_state.get('transcribed_question','')
    if transcribed:
        st.write('Transcription:',transcribed)
    question=typed.strip() or transcribed.strip()
    if st.button('Find answer in NSPIRE documents',disabled=not bool(question),use_container_width=True):
        with st.spinner('Searching locally uploaded references...'):
            matches=reference_matches(question)
        st.session_state['smart_results']=matches
        st.session_state['smart_query']=question
    if st.session_state.get('smart_query')==question and 'smart_results' in st.session_state:
        matches=st.session_state['smart_results']
        if not matches:
            st.warning('No matching text found in your local reference PDFs. No verified NSPIRE answer is available from this library.')
        else:
            st.markdown('#### Quick Reference Summary')

            first_match = matches[0]
            st.info(first_match['excerpt'][:500])

            st.caption(
                f"Source: {first_match['file']} — "
                f"PDF page {first_match['page']}"
            )

            st.warning(
                'This is a shortened source excerpt, not an official '
                'HUD compliance determination. Verify the full standard.'
            )

            st.markdown('#### Detailed HUD Reference Excerpts')

            spoken_parts=[]
            for result in matches:
                st.markdown(f"**{result['file']} — PDF page {result['page']}**")
                st.write(result['excerpt'])
                spoken_parts.append(f"{result['file']}, page {result['page']}. {result['excerpt'][:240]}")
            if spoken:
                speak_text(' '.join(spoken_parts))
            st.info('These are keyword-matched excerpts, not an AI-certified interpretation. Open the original publication to verify the precise deficiency criteria and effective version.')

elif page=='NSPIRE Reference Search':
    st.subheader('📚 Local NSPIRE Reference Library')
    st.warning('Only upload documents obtained from official HUD sources and confirm their effective dates. Search results are excerpts, not a guarantee that a standard applies.')
    pdf_dir=DATA/'nspire_references'
    pdf_dir.mkdir(exist_ok=True)
    uploaded=st.file_uploader('Add official NSPIRE PDF reference documents',type=['pdf'],accept_multiple_files=True)
    if uploaded and st.button('Save reference PDFs'):
        for f in uploaded:
            if f.size>25*1024*1024:
                st.error(f'{f.name} exceeds the 25 MB document limit.')
                continue
            safe=Path(f.name).name
            if not safe.lower().endswith('.pdf'):
                continue
            (pdf_dir/safe).write_bytes(f.getvalue())
            st.success(f'Saved {safe}')
    files=sorted(pdf_dir.glob('*.pdf'))
    st.write(f'{len(files)} reference PDF(s) installed.')
    for path in files:
        st.caption(path.name)
    q=st.text_input('Search reference text',placeholder='electrical outlet, smoke alarm, water leak...')
    if q.strip():
        matches=reference_matches(q)
        if not matches:
            st.info('No matches in the installed reference PDFs.')
        for item in matches:
            st.markdown(f"**{item['file']} — PDF page {item['page']}**")
            st.write(item['excerpt'])
        st.markdown(f'[Search official HUD materials on the web](https://www.google.com/search?q={quote("site:hud.gov NSPIRE " + q)})')
    st.markdown('[Official HUD NSPIRE information](https://www.hud.gov/program_offices/public_indian_housing/reac/nspire)')
    st.caption('PDF page numbers are file page numbers; printed page numbering may differ. Scanned image-only PDFs may not be searchable.')

elif page=='Report & Export':
    if not current():
        st.info('Open an inspection first.')
    else:
        inspection=rows('SELECT * FROM inspections WHERE id=?',(current(),))
        evidence=rows('SELECT * FROM evidence WHERE inspection_id=? ORDER BY captured_at,id',(current(),))
        if inspection:
            st.subheader('Inspection report')
            st.write(f"**{inspection[0]['property']}** — {len(evidence)} evidence images / frames")
            if evidence:
                try:
                    pdf=pdf_report(inspection[0],evidence)
                    st.download_button('Download inspector review PDF',pdf,file_name='HUD_AI_Inspector_Report.pdf',mime='application/pdf',use_container_width=True)
                except Exception as e:
                    st.error(f'Could not build PDF: {e}')
            else:
                st.info('Add evidence to generate a report.')
            total,counts=finding_summary(evidence)
            st.subheader('Inspection summary')
            c1,c2,c3=st.columns(3)
            c1.metric('Evidence',len(evidence))
            c2.metric('Potential observations',total)
            c3.metric('Not reviewed',counts.get('Not reviewed',0))
            for status,amount in sorted(counts.items()):
                st.write(f'{status}: {amount}')

            st.divider()
            st.subheader('Inspector Summary & Follow-up')
            st.caption('Based on saved inspector reviews. AI observations are suggestions, not official HUD/NSPIRE determinations.')
            if inspection[0].get('notes'):
                st.markdown('**General inspection notes**')
                st.write(inspection[0]['notes'])
            details = inspector_summary_details(evidence)
            pending = [d for d in details if d['status'] in ('Not reviewed', 'Needs closer inspection')]
            confirmed = [d for d in details if d['status'] == 'Confirmed observation']
            rejected = [d for d in details if d['status'] in ('Rejected AI observation', 'No deficiency observed')]
            sc1,sc2,sc3 = st.columns(3)
            sc1.metric('Requires follow-up', len(pending))
            sc2.metric('Confirmed by inspector', len(confirmed))
            sc3.metric('Rejected / no deficiency', len(rejected))
            if not details:
                st.info('No AI-suggested observations have been recorded. This does not establish a passing inspection.')
            else:
                for heading, group in [
                    ('Requires review or closer inspection', pending),
                    ('Inspector-confirmed observations', confirmed),
                    ('Rejected / no deficiency observed', rejected),
                ]:
                    st.markdown(f'#### {heading} ({len(group)})')
                    if not group:
                        st.caption('None recorded.')
                    for d in group:
                        with st.expander(f'{d["title"]} — {d["location"]} ({d["status"]})'):
                            st.write('**Inspector determination:**', d['status'])
                            st.write('**Field verification suggested:**', d['field_check'] or 'Not specified')
                            st.write('**Inspector notes:**', d['notes'] or 'None saved')
                            st.write('**Inspector-entered NSPIRE reference:**', d['reference'] or 'Not provided')
                            st.caption(f'Evidence source: {d["source"] or "Unknown"}')
                other = [d for d in details if d['status'] not in (
                    'Not reviewed', 'Needs closer inspection', 'Confirmed observation',
                    'Rejected AI observation', 'No deficiency observed')]
                if other:
                    st.markdown(f'#### Other determinations ({len(other)})')
                    for d in other:
                        st.write(f'{d["title"]} — {d["location"]}: {d["status"]}')
            if pending:
                st.warning('Follow-up is outstanding for one or more potential observations. Review each item in Review Findings before finalizing.')
            st.caption('This summary reflects only the evidence and reviews saved in this app. Verify the applicable official NSPIRE standards and perform required on-site checks.')
            st.divider()
            try:
                package=full_inspection_backup(inspection[0],evidence)
                st.download_button('Download complete inspection backup (ZIP with photos)',package,
                                   file_name='HUD_Inspection_Complete_Backup.zip',mime='application/zip',use_container_width=True)
            except Exception as e:
                st.error(f'Backup could not be created: {e}')
            backup={'format':'HUD_AI_INSPECTOR_M1_EXPORT','inspection':inspection[0],'evidence':[dict(x) for x in evidence]}
            st.download_button('Download inspection metadata (JSON)',json.dumps(backup,indent=2),file_name='inspection_metadata.json',mime='application/json',use_container_width=True)
            st.caption('JSON contains metadata and review text, not the original image files. Back up the inspection_data folder to preserve photos and video frames.')


elif page=='Phone Setup':
    st.subheader('Secure phone access')
    st.info('Camera and microphone permissions on a phone normally require HTTPS. Do not expose Streamlit port 8501 directly to the internet.')
    st.markdown('''**At home:** The application runs on your Windows PC. Keep that PC on and the app running.

**Away from home:** Use a private HTTPS tunnel or a properly secured hosting service. A recommended approach is Tailscale with HTTPS Serve, after configuring its prerequisites. Install and authenticate Tailscale on both Windows and your phone, enable HTTPS certificates for your tailnet, and follow the official Serve instructions. Do not enable public Funnel for resident inspection data.

**Before scanning:** Verify the URL shows HTTPS, sign in, and allow camera access. Confirm the correct inspection is open before collecting evidence.

**Security:** This is a single-password application, not a multi-inspector access-control system. Do not share your password, `.env`, or private inspection images. For deployment to multiple inspectors, additional authentication, auditing, and data protection are needed.''')
    st.link_button('Tailscale official Serve documentation','https://tailscale.com/kb/1312/serve')
    st.link_button('Streamlit official security documentation','https://docs.streamlit.io/develop/concepts/connections/security')
