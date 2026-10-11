"""HUD AI Inspector Milestone 4 — cloud TEST deployment (synthetic data only)."""
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
import sqlite3
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
DB = DATA / 'inspections.sqlite3'
MEDIA = DATA / 'media'
MEDIA.mkdir(exist_ok=True)

st.set_page_config(page_title='HUD AI Inspector | Milestone 4', page_icon='🏠', layout='wide')
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

def require_login():
    """Password gate for a single-inspector deployment; not multi-user authorization."""
    expected = os.getenv('HUD_INSPECTOR_PASSWORD', '').strip()
    if not expected:
        st.error('Security setup required: set HUD_INSPECTOR_PASSWORD in .env before using this version.')
        st.stop()
    if not st.session_state.get('authenticated', False):
        st.title('🔒 HUD AI Inspector — Sign in')
        with st.form('login'):
            supplied = st.text_input('Inspector password', type='password')
            clicked = st.form_submit_button('Sign in', use_container_width=True)
        if clicked:
            if hmac.compare_digest(supplied.encode(), expected.encode()):
                st.session_state['authenticated'] = True
                st.rerun()
            else:
                st.error('Incorrect password.')
        st.stop()


require_login()


def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('''CREATE TABLE IF NOT EXISTS inspections (id TEXT PRIMARY KEY, property TEXT NOT NULL, address TEXT, unit TEXT, inspector TEXT, inspected_on TEXT, notes TEXT, created_at TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id), area TEXT, room TEXT, kind TEXT, path TEXT, source_name TEXT, captured_at TEXT, findings_json TEXT, reviewed_json TEXT)''')
    connection.commit()
    return connection


def rows(sql, args=()):
    with db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql, args):
    with db() as c:
        c.execute(sql, args)
        c.commit()

def restore_inspection_backup(uploaded_file):
    """Validate and restore one portable inspection ZIP without overwriting data."""
    import stat

    max_zip_bytes = 100 * 1024 * 1024
    max_unpacked_bytes = 250 * 1024 * 1024
    max_file_bytes = 25 * 1024 * 1024
    inspection_columns = ('id', 'property', 'address', 'unit', 'inspector',
                          'inspected_on', 'notes', 'created_at')
    evidence_columns = ('id', 'inspection_id', 'area', 'room', 'kind', 'path',
                        'source_name', 'captured_at', 'findings_json', 'reviewed_json')

    if uploaded_file.size > max_zip_bytes:
        raise ValueError('ZIP backup exceeds the 100 MB upload limit.')

    with zipfile.ZipFile(io.BytesIO(uploaded_file.getvalue())) as archive:
        entries = archive.infolist()
        if len(entries) > 500:
            raise ValueError('ZIP contains too many entries.')
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError('ZIP contains duplicate entry names.')
        if 'inspection.json' not in names:
            raise ValueError('Backup is missing inspection.json.')
        if sum(entry.file_size for entry in entries) > max_unpacked_bytes:
            raise ValueError('ZIP uncompressed contents exceed 250 MB.')

        for entry in entries:
            name = entry.filename
            mode = (entry.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError('ZIP contains a symbolic link.')
            if entry.flag_bits & 0x1:
                raise ValueError('Encrypted ZIP entries are unsupported.')
            if entry.file_size > max_file_bytes and name != 'inspection.json':
                raise ValueError('A ZIP file exceeds the 25 MB per-photo limit.')
            if name == 'inspection.json':
                if entry.file_size > 5 * 1024 * 1024:
                    raise ValueError('Inspection metadata exceeds 5 MB.')
                continue
            parts = name.split('/')
            if (len(parts) != 2 or parts[0] != 'media' or not parts[1]
                    or parts[1] in ('.', '..') or '\\' in name or '\x00' in name
                    or entry.is_dir()):
                raise ValueError('ZIP contains an unexpected or unsafe file path.')

        metadata = json.loads(archive.read('inspection.json'))
        if not isinstance(metadata, dict) or metadata.get('format') != 'HUD_AI_INSPECTOR_M4_PORTABLE_BACKUP':
            raise ValueError('This is not a supported HUD AI Inspector backup.')
        inspection = metadata.get('inspection')
        evidence = metadata.get('evidence')
        if not isinstance(inspection, dict) or not isinstance(evidence, list):
            raise ValueError('Backup inspection or evidence data is invalid.')
        if len(evidence) > 499:
            raise ValueError('Backup contains too many evidence records.')
        if set(inspection) != set(inspection_columns):
            raise ValueError('Backup inspection fields do not match this app version.')
        inspection_id = inspection['id']
        if (not isinstance(inspection_id, str) or not inspection_id.strip()
                or len(inspection_id) > 100):
            raise ValueError('Backup has an invalid inspection ID.')
        if (not isinstance(inspection['property'], str)
                or not inspection['property'].strip()
                or len(inspection['property']) > 500):
            raise ValueError('Backup has an invalid property name.')
        for column in inspection_columns:
            value = inspection[column]
            if value is not None and (not isinstance(value, str) or len(value) > 10000):
                raise ValueError('Invalid inspection field: ' + column)

        evidence_ids = set()
        photo_names = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != set(evidence_columns):
                raise ValueError('Backup has an invalid evidence record.')
            eid = item['id']
            name = item['path']
            if (not isinstance(eid, str) or not eid.strip()
                    or len(eid) > 100 or eid in evidence_ids):
                raise ValueError('Backup has duplicate or invalid evidence IDs.')
            if item['inspection_id'] != inspection_id:
                raise ValueError('Evidence belongs to a different inspection.')
            if (not isinstance(name, str) or not name or len(name) > 255
                    or name in ('.', '..') or '/' in name or '\\' in name
                    or '\x00' in name or name in photo_names):
                raise ValueError('Backup has duplicate or unsafe photo filenames.')
            if 'media/' + name not in names:
                raise ValueError('Backup is missing photo: ' + name)
            for column in evidence_columns:
                value = item[column]
                if value is not None and (not isinstance(value, str) or len(value) > 100000):
                    raise ValueError('Invalid evidence field: ' + column)
            for column in ('findings_json', 'reviewed_json'):
                if item[column] is not None:
                    try:
                        json.loads(item[column])
                    except (TypeError, ValueError) as exc:
                        raise ValueError('Invalid ' + column + ' for evidence ' + eid) from exc
            evidence_ids.add(eid)
            photo_names.add(name)

        # Read and validate all photos before writing any records or files.
        photo_data = {}
        for name in photo_names:
            raw = archive.read('media/' + name)
            try:
                with Image.open(io.BytesIO(raw)) as img:
                    img.verify()
            except Exception as exc:
                raise ValueError('Backup contains an invalid image: ' + name) from exc
            photo_data[name] = raw

    MEDIA.mkdir(parents=True, exist_ok=True)
    created_photos = []
    connection = db()
    try:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM inspections WHERE id=?', (inspection_id,)).fetchone():
            raise ValueError('This inspection already exists. Restore cancelled.')
        for eid in evidence_ids:
            if connection.execute('SELECT 1 FROM evidence WHERE id=?', (eid,)).fetchone():
                raise ValueError('An evidence record already exists. Restore cancelled.')
        for name in photo_names:
            if (MEDIA / name).exists():
                raise ValueError('A photo filename already exists. Restore cancelled.')

        # Exclusive creation ensures existing photos are never overwritten.
        for name, raw in photo_data.items():
            with (MEDIA / name).open('xb') as destination:
                created_photos.append(MEDIA / name)
                destination.write(raw)

        connection.execute(
            'INSERT INTO inspections (' + ','.join(inspection_columns) + ') VALUES (' + ','.join('?' for _ in inspection_columns) + ')',
            tuple(inspection[column] for column in inspection_columns)
        )
        for item in evidence:
            connection.execute(
                'INSERT INTO evidence (' + ','.join(evidence_columns) + ') VALUES (' + ','.join('?' for _ in evidence_columns) + ')',
                tuple(item[column] for column in evidence_columns)
            )
        connection.commit()
    except Exception:
        connection.rollback()
        for photo in created_photos:
            photo.unlink(missing_ok=True)
        raise
    finally:
        connection.close()

    return inspection_id, len(evidence)


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


def save_photo(inspection_id, area, room, img, source, kind='photo'):
    eid = str(uuid.uuid4())
    name = eid + '.jpg'
    (MEDIA / name).write_bytes(image_bytes(img))
    execute('''INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (eid, inspection_id, area, room, kind, name, source, datetime.now().isoformat(timespec='seconds'), '[]', '{}'))
    return eid


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


def full_inspection_backup(inspection,evidence):
    """Portable ZIP with metadata and images, no passwords/API keys."""
    output=io.BytesIO()
    metadata={'format':'HUD_AI_INSPECTOR_M4_PORTABLE_BACKUP','inspection':inspection,'evidence':evidence}
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('inspection.json',json.dumps(metadata,indent=2))
        for item in evidence:
            name=Path(item.get('path') or '').name
            file=MEDIA/name
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
    for idx, item in enumerate(evidence,1):
        story.extend([PageBreak(), Paragraph(f'Evidence {idx}: {escape(item["area"] or "Unspecified")} / {escape(item["room"] or "Unspecified")}', styles['Heading2'])])
        if item['kind']=='video_frame':
            story.append(Paragraph(f'Video frame: {escape(item["source_name"] or "")}', styles['BodyText']))
        image_path = MEDIA / item['path']
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
st.caption('Milestone 4 • CLOUD TEST • Phone camera • AI screening • Voice • PDF summary • Temporary storage')
st.error('CLOUD TEST ONLY: Use invented property details and synthetic/non-resident photos. Free hosting can delete saved inspections at any time. Do not upload real resident data or private inspection evidence.')
st.warning('AI observations are not official NSPIRE findings. Confirm relevant HUD standards, perform required physical checks, and document your own determination.')

with st.sidebar:
    st.header('Inspection workspace')
    page = st.radio('Go to', ['Dashboard', 'New / Open Inspection', 'Capture & Upload', 'Live Camera Preview', 'Continuous Live Scanner', 'Review Findings', 'NSPIRE Smart Assistant', 'NSPIRE Reference Search', 'Report & Export', 'Phone Setup'], label_visibility='collapsed')
    st.caption('Single-inspector password protection is enabled. Use HTTPS and a private, secured deployment for cellular access.')
    if st.button('Sign out'):
        st.session_state['authenticated'] = False
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
                path=MEDIA/item['path']
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
            st.write(first_match['excerpt'][:500])

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
