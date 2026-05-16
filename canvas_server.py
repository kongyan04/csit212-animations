#!/usr/bin/env python3
"""
Canvas Final Project Submissions Server
- Browser fetches Canvas data via /canvas-proxy/ (server adds auth token)
- Comments + ratings stored in SQLite, served via /comments
- Announcements posted to all 3 sections via /announce
"""
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import json
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, jsonify, request, Response

# ── Configuration ─────────────────────────────────────────────────────────────

TOKEN       = '1925~QYBVrAy7KvKTHTCM2tG8ZTDQRx4fH2tDBvRE4YkzJxtCTzTyMecvP2YUz33E8ukC'
CANVAS_BASE = 'https://montclair.instructure.com'

SMTP_FROM = 'kongy@montclair.edu'
SMTP_HOST = 'smtp.office365.com'
SMTP_PORT = 587
SMTP_PASS = os.environ.get('MONTCLAIR_EMAIL_PASSWORD', 'QuFu1234!!')

ASSIGNMENTS = [
    {'label': 'Section 212615', 'display_id': 'CSIT212_15SP26', 'course_id': '212615', 'assignment_id': '2606377'},
    {'label': 'Section 212604', 'display_id': 'CSIT212_04SP26', 'course_id': '212604', 'assignment_id': '2606374'},
    {'label': 'Section 216874', 'display_id': 'CSIT212_74SP26', 'course_id': '216874', 'assignment_id': '2624407'},
]

SUPABASE_URL = 'https://gxcyitcuceimpsjpcdma.supabase.co'
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_uPxVTChmZItCG51gt8Angw_8Ii70pEj')

app = Flask(__name__)

# ── Database (Supabase REST API) ───────────────────────────────────────────────

def _sb_headers(extra=None):
    h = {
        'apikey':        SUPABASE_KEY,
        'Authorization': 'Bearer ' + SUPABASE_KEY,
        'Content-Type':  'application/json',
    }
    if extra:
        h.update(extra)
    return h

def _sb_get(table, params=''):
    url = f'{SUPABASE_URL}/rest/v1/{table}?{params}'
    req = urllib.request.Request(url, headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _sb_insert(table, data):
    url  = f'{SUPABASE_URL}/rest/v1/{table}'
    body = json.dumps(data).encode()
    req  = urllib.request.Request(url, data=body, method='POST',
                                   headers=_sb_headers({'Prefer': 'return=minimal'}))
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status

def _sb_delete(table, params):
    url = f'{SUPABASE_URL}/rest/v1/{table}?{params}'
    req = urllib.request.Request(url, method='DELETE', headers=_sb_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status

def db_add_comment(course_id, assignment_id, student_id, author_name, author_email, body):
    _sb_insert('comments', {
        'course_id': course_id, 'assignment_id': assignment_id,
        'student_id': student_id, 'author_name': author_name,
        'author_email': author_email, 'body': body,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })

def db_add_rating(course_id, assignment_id, student_id, score):
    _sb_insert('ratings', {
        'course_id': course_id, 'assignment_id': assignment_id,
        'student_id': student_id, 'score': score,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })

def db_all_comments():
    rows = _sb_get('comments', 'select=*&order=created_at.asc')
    result = {}
    for r in rows:
        key = f"{r['course_id']}|{r['assignment_id']}|{r['student_id']}"
        result.setdefault(key, []).append(r)
    return result

def db_all_ratings():
    rows = _sb_get('ratings', 'select=*')
    agg  = {}
    for r in rows:
        key = f"{r['course_id']}|{r['assignment_id']}|{r['student_id']}"
        agg.setdefault(key, []).append(r['score'])
    return {k: {'avg': round(sum(v)/len(v), 1), 'count': len(v)} for k, v in agg.items()}

# ── Canvas helpers ─────────────────────────────────────────────────────────────

def canvas_request(method, path, body=None, user_token=None, user_base=None, content_type=None):
    base = user_base or CANVAS_BASE
    url = base + path
    token = user_token or TOKEN
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), resp.headers.get('Link', '')
    except urllib.error.HTTPError as e:
        return e.code, e.read(), ''

def canvas_post_comment(course_id, assignment_id, student_id, text):
    body = urllib.parse.urlencode({'comment[text_body]': text}).encode()
    code, _, __ = canvas_request('PUT',
        f'/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/{student_id}', body)
    return code < 300, f'Canvas error {code}' if code >= 300 else None

def canvas_post_announcement(course_id, title, message):
    body = urllib.parse.urlencode({
        'title': title, 'message': message,
        'is_announcement': 'true', 'published': 'true',
    }).encode()
    code, data, _ = canvas_request('POST', f'/api/v1/courses/{course_id}/discussion_topics', body)
    if code < 300:
        return True, json.loads(data).get('html_url', '')
    return False, f'Canvas error {code}: {data.decode()}'

# ── Email helper ───────────────────────────────────────────────────────────────

def send_email(to_addr, student_name, commenter_name, comment_body, project_url):
    if not SMTP_PASS:
        return False, 'No SMTP password'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'New comment on your Final Project – CSIT 212'
        msg['From']    = f'Prof. Kong – CSIT 212 <{SMTP_FROM}>'
        msg['To']      = to_addr
        text = (f'Hi {student_name},\n\n{commenter_name} left a comment on your Final Project:\n\n'
                f'"{comment_body}"\n\nView: {project_url}\n\n— CSIT 212')
        html = f'''<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
  <div style="background:#007acc;padding:20px 28px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:1.1rem">New comment on your Final Project</h2>
  </div>
  <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;padding:24px 28px;border-radius:0 0 8px 8px">
    <p style="color:#555;margin:0 0 16px">Hi <strong>{student_name}</strong>,</p>
    <p style="color:#555;margin:0 0 16px"><strong>{commenter_name}</strong> left a comment on your Final Project:</p>
    <blockquote style="border-left:4px solid #007acc;margin:0 0 20px;padding:12px 16px;background:#f0f8ff;color:#333">
      {comment_body}
    </blockquote>
    <a href="{project_url}" style="display:inline-block;background:#007acc;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold">View all comments</a>
  </div>
</div>'''
        msg.attach(MIMEText(text, 'plain'))
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo(); s.starttls()
            s.login(SMTP_FROM, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        return True, None
    except Exception as e:
        print(f'[email] {e}')
        return False, str(e)

# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.response_class(HTML, mimetype='text/html')

@app.route('/dashboard')
def dashboard():
    with open('professor_dashboard.html', 'r') as f:
        html = f.read()
    return app.response_class(html, mimetype='text/html')


@app.route('/canvas-proxy/<path:canvas_path>', methods=['GET', 'PUT', 'POST', 'DELETE', 'OPTIONS'])
def canvas_proxy(canvas_path):
    """Forward requests to Canvas with server-side or user-supplied auth token.

    User can supply their own credentials via headers:
      X-Canvas-Token   — their Canvas API token (used instead of server default)
      X-Canvas-Domain  — their Canvas instance, e.g. https://harvard.instructure.com
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        resp = Response('', status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, PUT, POST, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Canvas-Token, X-Canvas-Domain'
        return resp

    qs       = request.query_string.decode()
    path     = '/' + canvas_path + ('?' + qs if qs else '')
    req_body = request.get_data() if request.method in ('PUT', 'POST') else None
    user_token  = request.headers.get('X-Canvas-Token')
    user_domain = request.headers.get('X-Canvas-Domain')
    ct          = request.headers.get('Content-Type')

    code, body, link = canvas_request(
        request.method, path, req_body,
        user_token=user_token, user_base=user_domain, content_type=ct
    )

    base_for_rewrite = user_domain or CANVAS_BASE
    if link:
        link = link.replace(base_for_rewrite, request.host_url.rstrip('/') + '/canvas-proxy')

    resp = Response(body, status=code, mimetype='application/json')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, PUT, POST, DELETE, OPTIONS'
    resp.headers['Access-Control-Expose-Headers'] = 'Link'
    if link:
        resp.headers['Link'] = link
    return resp

@app.route('/comments')
def get_comments():
    return jsonify({'comments': db_all_comments(), 'ratings': db_all_ratings()})

@app.route('/comment', methods=['POST'])
def post_comment():
    d             = request.get_json(force=True)
    course_id     = str(d.get('course_id',     '')).strip()
    assignment_id = str(d.get('assignment_id', '')).strip()
    student_id    = str(d.get('student_id',    '')).strip()
    author_name   = str(d.get('author_name',   '')).strip()[:100]
    author_email  = str(d.get('author_email',  '')).strip()[:200]
    body          = str(d.get('body',          '')).strip()[:2000]
    student_name  = str(d.get('student_name',  'Student')).strip()
    student_email = str(d.get('student_email', '')).strip()

    if not all([course_id, assignment_id, student_id, author_name, body]):
        return jsonify({'ok': False, 'error': 'Missing fields'}), 400
    known = {(a['course_id'], a['assignment_id']) for a in ASSIGNMENTS}
    if (course_id, assignment_id) not in known:
        return jsonify({'ok': False, 'error': 'Unknown assignment'}), 403

    db_add_comment(course_id, assignment_id, student_id, author_name, author_email, body)

    canvas_text = f'[Peer comment from {author_name}]: {body}'
    canvas_ok, canvas_err = canvas_post_comment(course_id, assignment_id, student_id, canvas_text)
    if not canvas_ok:
        print(f'[canvas comment] {canvas_err}')

    if student_email:
        send_email(student_email, student_name, author_name, body,
                   request.host_url.rstrip('/'))

    return jsonify({'ok': True, 'canvas_ok': canvas_ok})

@app.route('/rate', methods=['POST'])
def post_rating():
    d             = request.get_json(force=True)
    course_id     = str(d.get('course_id',     '')).strip()
    assignment_id = str(d.get('assignment_id', '')).strip()
    student_id    = str(d.get('student_id',    '')).strip()
    try:
        score = int(d.get('score'))
        assert 1 <= score <= 10
    except Exception:
        return jsonify({'ok': False, 'error': 'Score must be 1–10'}), 400
    known = {(a['course_id'], a['assignment_id']) for a in ASSIGNMENTS}
    if (course_id, assignment_id) not in known:
        return jsonify({'ok': False, 'error': 'Unknown assignment'}), 403

    db_add_rating(course_id, assignment_id, student_id, score)
    key    = f'{course_id}|{assignment_id}|{student_id}'
    rating = db_all_ratings().get(key, {'avg': score, 'count': 1})
    return jsonify({'ok': True, 'rating': rating})

# ── Announcements ──────────────────────────────────────────────────────────────

@app.route('/announce')
def announce_page():
    return app.response_class(ANNOUNCE_HTML, mimetype='text/html')

@app.route('/announce', methods=['POST'])
def announce_post():
    d          = request.get_json(force=True)
    title      = str(d.get('title', '')).strip()
    body       = str(d.get('body',  '')).strip()
    course_ids = d.get('course_ids') or [a['course_id'] for a in ASSIGNMENTS]
    if not title or not body:
        return jsonify({'ok': False, 'error': 'Title and body are required.'}), 400
    results = []
    for cfg in ASSIGNMENTS:
        if cfg['course_id'] not in course_ids:
            continue
        ok, info = canvas_post_announcement(cfg['course_id'], title, body)
        results.append({'label': cfg['label'], 'ok': ok, 'info': info})
    return jsonify({'ok': all(r['ok'] for r in results), 'results': results})

# ── Banner Grade Submission ────────────────────────────────────────────────────

# ── Anthropic API proxy (for AI-powered task planning) ─────────────────────────

@app.route('/ai-route', methods=['POST', 'OPTIONS'])
def ai_route():
    """Proxy to Anthropic API for natural language task planning."""
    if request.method == 'OPTIONS':
        resp = Response('', status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Anthropic-Key'
        return resp

    api_key = request.headers.get('X-Anthropic-Key', '').strip()
    if not api_key:
        return jsonify({'ok': False, 'error': 'X-Anthropic-Key header required'}), 400

    body = request.get_data()
    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=body, method='POST',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            resp = Response(data, status=r.status, mimetype='application/json')
    except urllib.error.HTTPError as e:
        resp = Response(e.read(), status=e.code, mimetype='application/json')
    except Exception as e:
        resp = jsonify({'ok': False, 'error': str(e)}); resp.status_code = 500
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/submit-banner-grades', methods=['POST', 'OPTIONS'])
def submit_banner_grades():
    """Accept grade data and store for Banner submission."""
    if request.method == 'OPTIONS':
        resp = Response('', status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    d = request.get_json(force=True)
    course_id = d.get('course_id', '')
    crn       = d.get('crn', '')
    term      = d.get('term', '202620')
    grades    = d.get('grades', [])

    if not grades:
        return jsonify({'ok': False, 'error': 'No grades provided.'}), 400

    # Store grades to a JSON file for the Playwright script to pick up
    import os
    grade_file = os.path.join(os.path.dirname(__file__), f'pending_grades_{course_id}.json')
    with open(grade_file, 'w') as f:
        json.dump({'course_id': course_id, 'crn': crn, 'term': term, 'grades': grades,
                   'submitted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, f, indent=2)

    # Also store in Supabase for persistence
    try:
        _sb_insert('final_grades', {
            'course_id': course_id, 'crn': crn, 'term': term,
            'grades': json.dumps(grades),
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        })
    except:
        pass  # Supabase is optional

    resp = jsonify({
        'ok': True,
        'message': f'Saved {len(grades)} grades for CRN {crn}. '
                   f'Run "python3 nest_scraper.py submit-grades {course_id}" to push to Banner, '
                   f'or use the exported CSV to enter grades manually in Banner SSB.',
        'file': grade_file,
        'count': len(grades),
    })
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ── Keep-alive (prevent Render free tier sleep) ────────────────────────────────

def keep_alive():
    time.sleep(60)
    while True:
        try:
            urllib.request.urlopen('https://csit212-submissions.onrender.com/', timeout=10)
            print('[keepalive] pinged self')
        except Exception as e:
            print(f'[keepalive] {e}')
        time.sleep(10 * 60)

# ── HTML template ──────────────────────────────────────────────────────────────

HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Final Project Submissions – CSIT 212</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f5f7fa; color: #333; font-family: "Segoe UI", Arial, sans-serif; min-height: 100vh; }

  header { background: #007acc; padding: 28px 40px 22px; text-align: center; }
  header h1 { font-size: 1.9rem; color: #fff; font-weight: 800; }
  header p  { color: #cce7ff; font-size: 0.9rem; margin-top: 5px; }

  .meta-bar { background: #e8f4fd; border-bottom: 1px solid #bee3f8; padding: 8px 32px;
    font-size: 0.8rem; color: #2b6cb0; display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #48bb78; display: inline-block; }
  .dot.loading { background: #ed8936; animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .countdown { margin-left: auto; }

  main { max-width: 1200px; margin: 0 auto; padding: 28px 24px 60px; }

  .section-header { display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px; margin: 32px 0 10px; }
  .section-title { font-size: 1.05rem; font-weight: 700; color: #007acc;
    border-left: 4px solid #007acc; padding-left: 12px; }
  .section-meta { font-size: 0.82rem; color: #888; }

  .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
  .toolbar input, .toolbar select { padding: 7px 12px; border: 1px solid #ccc;
    border-radius: 8px; font-size: 0.875rem; background: #fff; outline: none; font-family: inherit; }
  .toolbar input { width: 220px; }
  .toolbar input:focus, .toolbar select:focus { border-color: #007acc; }
  .count-badge { margin-left: auto; font-size: 0.8rem; color: #999; }

  .table-wrap { background: #fff; border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.07); overflow: hidden; margin-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; }
  thead th { background: #007acc; color: #fff; padding: 11px 14px; text-align: left;
    font-size: 0.77rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
    cursor: pointer; user-select: none; white-space: nowrap; }
  thead th:hover { background: #005fa3; }
  thead th .si { margin-left: 4px; opacity: 0.45; }
  thead th.sorted .si { opacity: 1; }
  tbody tr { border-bottom: 1px solid #f0f0f0; transition: background 0.1s; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: #f8fbff; }
  tbody td { padding: 10px 14px; font-size: 0.875rem; vertical-align: top; }

  .student-name  { font-weight: 600; color: #222; }
  .student-login { font-size: 0.77rem; color: #aaa; }

  .link-cell a { color: #007acc; text-decoration: none; font-size: 0.82rem;
    word-break: break-all; display: block; margin-bottom: 2px; }
  .link-cell a:hover { text-decoration: underline; }
  .no-link { color: #ccc; font-style: italic; font-size: 0.82rem; }

  .pill { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.72rem; font-weight: 700; }
  .pill-green { background: #c6f6d5; color: #276749; }
  .pill-blue  { background: #bee3f8; color: #2a69ac; }
  .pill-red   { background: #fed7d7; color: #9b2c2c; }
  .pill-gray  { background: #eee;    color: #777; }

  /* Comment cell */
  .comment-cell { min-width: 180px; }
  .comment-list { margin-bottom: 8px; }
  .comment-item { background: #f0f8ff; border-left: 3px solid #bee3f8; border-radius: 0 6px 6px 0;
    padding: 7px 10px; margin-bottom: 6px; font-size: 0.8rem; }
  .comment-author { font-weight: 700; color: #2b6cb0; }
  .comment-time   { font-size: 0.72rem; color: #aaa; margin-left: 6px; }
  .comment-body   { color: #444; margin-top: 3px; line-height: 1.4; }
  .add-comment-btn { background: none; border: 1px dashed #007acc; color: #007acc;
    border-radius: 6px; padding: 4px 10px; font-size: 0.78rem; cursor: pointer;
    transition: all 0.15s; white-space: nowrap; }
  .add-comment-btn:hover { background: #007acc; color: #fff; }

  /* Rating */
  .rating-cell { min-width: 120px; }
  .rate-btn { background: none; border: 1px dashed #f6ad55; color: #c07a00;
    border-radius: 6px; padding: 4px 10px; font-size: 0.78rem; cursor: pointer;
    transition: all 0.15s; white-space: nowrap; margin-top: 5px; display: inline-block; }
  .rate-btn:hover { background: #f6ad55; color: #fff; }

  /* Modal shared */
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    display: flex; align-items: center; justify-content: center; z-index: 1000;
    opacity: 0; pointer-events: none; transition: opacity 0.15s; }
  .modal-backdrop.open { opacity: 1; pointer-events: all; }
  .modal { background: #fff; border-radius: 14px; box-shadow: 0 8px 40px rgba(0,0,0,0.18);
    width: 100%; max-width: 460px; padding: 28px; transform: translateY(12px);
    transition: transform 0.15s; }
  .modal-backdrop.open .modal { transform: translateY(0); }
  .modal h3 { font-size: 1rem; font-weight: 700; color: #007acc; margin-bottom: 6px; }
  .modal .target-name { font-size: 0.82rem; color: #888; margin-bottom: 18px; }
  .modal label { font-size: 0.78rem; font-weight: 700; color: #555;
    text-transform: uppercase; letter-spacing: 0.07em; display: block; margin-bottom: 4px; margin-top: 14px; }
  .modal input, .modal textarea { width: 100%; padding: 9px 12px; border: 1px solid #ccc;
    border-radius: 8px; font-size: 0.9rem; font-family: inherit; outline: none; resize: vertical; }
  .modal input:focus, .modal textarea:focus { border-color: #007acc; box-shadow: 0 0 0 2px rgba(0,122,204,0.15); }
  .modal textarea { min-height: 100px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
  .btn { padding: 9px 22px; border: none; border-radius: 8px; font-size: 0.875rem;
    font-weight: 700; cursor: pointer; transition: background 0.15s; }
  .btn-primary { background: #007acc; color: #fff; }
  .btn-primary:hover { background: #005fa3; }
  .btn-primary:disabled { background: #a0c8e8; cursor: not-allowed; }
  .btn-secondary { background: #eee; color: #555; }
  .btn-secondary:hover { background: #ddd; }
  .modal-error   { color: #c53030; font-size: 0.82rem; margin-top: 10px; }
  .modal-success { color: #276749; font-size: 0.82rem; margin-top: 10px; background: #c6f6d5;
    border-radius: 6px; padding: 8px 12px; }

  /* Rating modal stars */
  .rating-stars-row { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap; margin: 18px 0 8px; }
  .rstar { width: 38px; height: 38px; border-radius: 8px; border: 2px solid #e2e8f0;
    background: #f7fafc; font-size: 1rem; font-weight: 700; color: #555; cursor: pointer;
    transition: all 0.12s; display: flex; align-items: center; justify-content: center; }
  .rstar:hover, .rstar.active { background: #f6ad55; border-color: #f6ad55; color: #fff; }
  .rstar.selected { background: #007acc; border-color: #007acc; color: #fff; }

  .empty-state { text-align: center; padding: 48px 20px; color: #bbb; font-size: 0.9rem; }
  .loading-state { text-align: center; padding: 60px 20px; color: #999; }
  .spinner { width: 36px; height: 36px; border: 4px solid #e2e8f0; border-top-color: #007acc;
    border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 14px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>Final Project Submissions</h1>
  <p>CSIT 212 &nbsp;&middot;&nbsp; Spring 2026</p>
</header>

<div class="meta-bar">
  <span><span class="dot loading" id="statusDot"></span>&nbsp;<span id="statusText">Loading…</span></span>
  <span id="lastUpdated"></span>
  <span class="countdown" id="countdown"></span>
</div>

<main id="main">
  <div class="loading-state"><div class="spinner"></div>Fetching submissions…</div>
</main>

<!-- Comment Modal -->
<div class="modal-backdrop" id="modalBackdrop" onclick="closeModal(event)">
  <div class="modal">
    <h3>Leave a Comment</h3>
    <div class="target-name" id="modalTargetName"></div>
    <label>Your Name *</label>
    <input type="text" id="modalAuthorName" placeholder="Enter your name">
    <label>Your Email (optional — not shown publicly)</label>
    <input type="email" id="modalAuthorEmail" placeholder="your@email.com">
    <label>Comment *</label>
    <textarea id="modalBody" placeholder="Write your feedback or comment…"></textarea>
    <div class="modal-error"   id="modalError"   style="display:none"></div>
    <div class="modal-success" id="modalSuccess" style="display:none"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary"   id="modalSubmitBtn" onclick="submitComment()">Post Comment</button>
    </div>
  </div>
</div>

<!-- Rating Modal -->
<div class="modal-backdrop" id="ratingBackdrop" onclick="closeRatingModal(event)">
  <div class="modal" style="max-width:400px;text-align:center">
    <h3>Rate this Project</h3>
    <div class="target-name" id="ratingTargetName"></div>
    <div class="rating-stars-row" id="ratingStarsRow"></div>
    <div style="font-size:0.82rem;color:#aaa;margin-bottom:4px">Click a number to select your rating</div>
    <div class="modal-error"   id="ratingError"   style="display:none;text-align:left"></div>
    <div class="modal-success" id="ratingSuccess" style="display:none;text-align:left"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeRatingModal()">Cancel</button>
      <button class="btn btn-primary" id="ratingSubmitBtn" onclick="submitRating()">Submit Rating</button>
    </div>
  </div>
</div>

<script>
const ASSIGNMENTS = ''' + json.dumps([{**a} for a in ASSIGNMENTS]) + r''';
let allSections = [];   // [{label,course_id,assignment_id,assign_name,due_at,rows}]
let commentsMap = {};   // "cid|aid|sid" -> [comments]
let ratingsMap  = {};   // "cid|aid|sid" -> {avg,count}
let sortState   = {};
let modalCtx    = null;
let ratingCtx   = null;

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function loadAll() {
  document.getElementById('statusDot').className    = 'dot loading';
  document.getElementById('statusText').textContent = 'Loading submissions…';
  try {
    // Fetch Canvas data for all 3 assignments via server proxy (parallel)
    const sectionPromises = ASSIGNMENTS.map(a => fetchSection(a));
    const commentsProm    = fetch('/comments').then(r => r.json());

    allSections = await Promise.all(sectionPromises);
    const cd    = await commentsProm;
    commentsMap = cd.comments || {};
    ratingsMap  = cd.ratings  || {};

    document.getElementById('statusDot').className    = 'dot';
    document.getElementById('statusText').textContent = 'Live';
    document.getElementById('lastUpdated').textContent =
      'Updated ' + new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});

    renderAll();
    setTimeout(loadAll, 30 * 60 * 1000);
    startCountdown(30 * 60);
  } catch(e) {
    document.getElementById('statusText').textContent = 'Error: ' + e.message;
    document.getElementById('statusDot').className    = 'dot loading';
    setTimeout(loadAll, 20000);
  }
}

async function fetchSection(cfg) {
  // 1) Get assignment info
  const asgn = await proxyGet(
    `/api/v1/courses/${cfg.course_id}/assignments/${cfg.assignment_id}`);

  // 2) Get all submissions (paginated)
  const subs = await proxyGetAll(
    `/api/v1/courses/${cfg.course_id}/assignments/${cfg.assignment_id}/submissions?per_page=100&include[]=user`);

  const rows = subs.map(sub => {
    const student = sub.user || {};
    const links   = [];
    if (sub.url) links.push(sub.url);
    (sub.attachments || []).forEach(att => {
      if (att.url && !att.url.includes('instructure.com')) links.push(att.url);
    });
    const login = student.login_id || '';
    return {
      student_id:     String(sub.user_id || ''),
      name:           student.name  || 'Unknown',
      login:          login,
      email:          student.email || (login ? login + '@mail.montclair.edu' : ''),
      links,
      submitted_at:   sub.submitted_at,
      workflow_state: sub.workflow_state,
      late:           sub.late    || false,
      missing:        sub.missing || false,
    };
  });

  rows.sort((a,b) => {
    if (!!a.submitted_at !== !!b.submitted_at) return a.submitted_at ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return {
    label:         cfg.label,
    display_id:    cfg.display_id || cfg.label,
    course_id:     cfg.course_id,
    assignment_id: cfg.assignment_id,
    assign_name:   asgn.name || '',
    due_at:        asgn.due_at,
    rows,
    submitted:     rows.filter(r => r.submitted_at).length,
    total:         rows.length,
  };
}

async function proxyGet(path) {
  const res = await fetch('/canvas-proxy' + path);
  if (!res.ok) throw new Error('Canvas proxy error ' + res.status);
  return res.json();
}

async function proxyGetAll(path) {
  let results = [], nextPath = path;
  while (nextPath) {
    const res  = await fetch('/canvas-proxy' + nextPath);
    if (!res.ok) throw new Error('Canvas proxy error ' + res.status);
    const data = await res.json();
    results    = results.concat(Array.isArray(data) ? data : [data]);
    const link = res.headers.get('Link') || '';
    const m    = link.match(/<([^>]+)>;[^,]*rel="next"/);
    if (m) {
      // Strip the host/proxy prefix to get just the path
      nextPath = m[1].replace(/^https?:\/\/[^/]+\/canvas-proxy/, '');
    } else {
      nextPath = null;
    }
  }
  return results;
}

function startCountdown(seconds) {
  clearInterval(window._cdTimer);
  let s = seconds;
  const el = document.getElementById('countdown');
  window._cdTimer = setInterval(() => {
    if (--s <= 0) { clearInterval(window._cdTimer); return; }
    const m = Math.floor(s/60), sec = s%60;
    el.textContent = `Refreshes in ${m}:${String(sec).padStart(2,'0')}`;
  }, 1000);
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderAll() {
  const main = document.getElementById('main');
  main.innerHTML = '';
  allSections.forEach(s => renderSection(s));
}

function renderSection(section) {
  const main = document.getElementById('main');
  if (!sortState[section.label]) sortState[section.label] = {col:-1, asc:true};
  const id  = 'sec-' + section.label.replace(/\s+/g,'_');
  const due = section.due_at
    ? 'Due ' + new Date(section.due_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '';

  const div = document.createElement('div');
  div.id = id;
  div.innerHTML = `
    <div class="section-header">
      <div class="section-title">${esc(section.display_id||section.label)} &nbsp;&ndash;&nbsp; ${esc(section.assign_name)}</div>
      <div class="section-meta">${section.submitted} submitted &nbsp;/&nbsp; ${section.total} students${due?' &nbsp; '+due:''}</div>
    </div>
    <div class="toolbar">
      <input type="text" id="search-${id}" placeholder="Search student…" oninput="filter('${id}','${section.label}')">
      <select id="status-${id}" onchange="filter('${id}','${section.label}')">
        <option value="">All Statuses</option>
        <option value="submitted">Has link</option>
        <option value="unsubmitted">No submission</option>
        <option value="graded">Graded</option>
        <option value="late">Late</option>
      </select>
      <span class="count-badge" id="count-${id}"></span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr>
        <th onclick="sortSec('${id}','${section.label}',0)">Student <span class="si">↕</span></th>
        <th>Submission Link</th>
        <th>Status</th>
        <th>Rating</th>
        <th>Comments</th>
      </tr></thead>
      <tbody id="tbody-${id}"></tbody>
    </table></div>`;
  main.appendChild(div);
  filter(id, section.label);
}

function getSection(label) { return allSections.find(s => s.label === label); }

function filter(id, label) {
  const sec    = getSection(label); if (!sec) return;
  const search = (document.getElementById('search-' + id)?.value || '').toLowerCase();
  const status = document.getElementById('status-' + id)?.value || '';
  let rows = sec.rows.filter(r => {
    if (search && !r.name.toLowerCase().includes(search) && !r.login.toLowerCase().includes(search)) return false;
    if (status === 'submitted'   && !r.submitted_at) return false;
    if (status === 'unsubmitted' && r.workflow_state !== 'unsubmitted') return false;
    if (status === 'graded'      && r.workflow_state !== 'graded') return false;
    if (status === 'late'        && !r.late) return false;
    return true;
  });
  const st = sortState[label];
  if (st && st.col >= 0) rows = doSort(rows, st.col, st.asc);
  document.getElementById('count-'+id).textContent = rows.length + ' student' + (rows.length!==1?'s':'');
  renderRows('tbody-'+id, rows, sec);
}

function sortSec(id, label, col) {
  const st = sortState[label];
  if (st.col === col) st.asc = !st.asc; else { st.col = col; st.asc = true; }
  document.querySelectorAll('#'+id+' thead th').forEach(th => {
    th.classList.remove('sorted');
    const si = th.querySelector('.si'); if (si) si.textContent = '↕';
  });
  const ths = document.querySelectorAll('#'+id+' thead th');
  if (ths[col]) { ths[col].classList.add('sorted'); const si = ths[col].querySelector('.si'); if(si) si.textContent = st.asc?'↑':'↓'; }
  filter(id, label);
}

function doSort(rows, col, asc) {
  return [...rows].sort((a,b) => {
    let va, vb;
    if (col===0) { va=a.name; vb=b.name; } else return 0;
    return va<vb?(asc?-1:1):va>vb?(asc?1:-1):0;
  });
}

function renderRows(tbodyId, rows, section) {
  const tbody = document.getElementById(tbodyId); if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state">No students match.</div></td></tr>'; return;
  }
  tbody.innerHTML = rows.map(r => {
    const linkHtml = r.links.length
      ? r.links.map(l => `<a href="${ea(l)}" target="_blank" rel="noopener">${esc(l)}</a>`).join('')
      : '<span class="no-link">—</span>';

    let pill = '';
    if      (r.missing)                      pill='<span class="pill pill-red">Missing</span>';
    else if (r.late)                         pill='<span class="pill pill-red">Late</span>';
    else if (r.workflow_state==='graded')    pill='<span class="pill pill-green">Graded</span>';
    else if (r.workflow_state==='submitted') pill='<span class="pill pill-blue">Submitted</span>';
    else                                     pill='<span class="pill pill-gray">Not Submitted</span>';

    const key      = `${section.course_id}|${section.assignment_id}|${r.student_id}`;
    const comments = commentsMap[key] || [];
    const rating   = ratingsMap[key]  || {avg: null, count: 0};
    const rcid     = 'rc-' + section.course_id + '-' + r.student_id;

    const commentHtml = comments.map(c => `
      <div class="comment-item">
        <span class="comment-author">${esc(c.author_name)}</span>
        <span class="comment-time">${fmtDate(c.created_at)}</span>
        <div class="comment-body">${esc(c.body)}</div>
      </div>`).join('');

    const btnLabel = comments.length ? `💬 ${comments.length} comment${comments.length>1?'s':''}` : '+ Add comment';

    const avgStr = rating.avg != null
      ? `<span style="font-size:1.1rem;font-weight:800;color:#007acc">${rating.avg}</span><span style="font-size:0.75rem;color:#aaa">/10</span> <span style="font-size:0.72rem;color:#bbb">(${rating.count} vote${rating.count!==1?'s':''})</span>`
      : '<span style="color:#ccc;font-size:0.8rem">No ratings yet</span>';

    return `<tr>
      <td><div class="student-name">${esc(fmtName(r.name))}</div><div class="student-login">${esc(r.login)}</div></td>
      <td class="link-cell">${linkHtml}</td>
      <td>${pill}</td>
      <td class="rating-cell">
        <div id="${rcid}">${avgStr}</div>
        <button class="rate-btn" onclick="openRatingModal('${esc(section.course_id)}','${esc(section.assignment_id)}','${esc(r.student_id)}','${esc(fmtName(r.name))}','${rcid}')">⭐ Rate</button>
      </td>
      <td class="comment-cell">
        <div class="comment-list">${commentHtml}</div>
        <button class="add-comment-btn" onclick="openModal('${esc(section.course_id)}','${esc(section.assignment_id)}','${esc(r.student_id)}','${esc(fmtName(r.name))}','${esc(r.email)}')">
          ${btnLabel}</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Comment Modal ─────────────────────────────────────────────────────────────

function openModal(courseId, assignmentId, studentId, studentName, studentEmail) {
  modalCtx = {courseId, assignmentId, studentId, studentName, studentEmail};
  document.getElementById('modalTargetName').textContent = "On: " + studentName + "'s project";
  document.getElementById('modalAuthorName').value  = '';
  document.getElementById('modalAuthorEmail').value = '';
  document.getElementById('modalBody').value        = '';
  document.getElementById('modalError').style.display   = 'none';
  document.getElementById('modalSuccess').style.display = 'none';
  document.getElementById('modalSubmitBtn').disabled    = false;
  document.getElementById('modalSubmitBtn').textContent = 'Post Comment';
  document.getElementById('modalBackdrop').classList.add('open');
  setTimeout(() => document.getElementById('modalAuthorName').focus(), 50);
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modalBackdrop')) return;
  document.getElementById('modalBackdrop').classList.remove('open');
}

async function submitComment() {
  if (!modalCtx) return;
  const authorName  = document.getElementById('modalAuthorName').value.trim();
  const authorEmail = document.getElementById('modalAuthorEmail').value.trim();
  const body        = document.getElementById('modalBody').value.trim();
  const errEl = document.getElementById('modalError');
  const okEl  = document.getElementById('modalSuccess');
  errEl.style.display = 'none'; okEl.style.display = 'none';
  if (!authorName) { errEl.textContent='Please enter your name.'; errEl.style.display='block'; return; }
  if (!body)       { errEl.textContent='Please write a comment.'; errEl.style.display='block'; return; }
  const btn = document.getElementById('modalSubmitBtn');
  btn.disabled = true; btn.textContent = 'Posting…';
  try {
    const res  = await fetch('/comment', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        course_id: modalCtx.courseId, assignment_id: modalCtx.assignmentId,
        student_id: modalCtx.studentId, author_name: authorName,
        author_email: authorEmail, body,
        student_name: modalCtx.studentName, student_email: modalCtx.studentEmail,
      })
    });
    const json = await res.json();
    if (json.ok) {
      const key = `${modalCtx.courseId}|${modalCtx.assignmentId}|${modalCtx.studentId}`;
      commentsMap[key] = commentsMap[key] || [];
      commentsMap[key].push({author_name: authorName, body, created_at: new Date().toISOString()});
      okEl.textContent = '✓ Comment posted' + (json.canvas_ok ? ' and added to Canvas.' : '.');
      okEl.style.display = 'block';
      btn.textContent = 'Posted!';
      setTimeout(() => {
        document.getElementById('modalBackdrop').classList.remove('open');
        renderAll();
      }, 1400);
    } else {
      errEl.textContent = json.error || 'Failed to post.';
      errEl.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Post Comment';
    }
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
    errEl.style.display = 'block';
    btn.disabled = false; btn.textContent = 'Post Comment';
  }
}

// ── Rating Modal ──────────────────────────────────────────────────────────────

function openRatingModal(courseId, assignmentId, studentId, studentName, cid) {
  ratingCtx = {courseId, assignmentId, studentId, studentName, cid, score: null};
  document.getElementById('ratingTargetName').textContent = studentName + "'s project";
  document.getElementById('ratingError').style.display   = 'none';
  document.getElementById('ratingSuccess').style.display = 'none';
  document.getElementById('ratingSubmitBtn').disabled    = false;
  document.getElementById('ratingSubmitBtn').textContent = 'Submit Rating';
  const row = document.getElementById('ratingStarsRow');
  row.innerHTML = [1,2,3,4,5,6,7,8,9,10].map(n =>
    `<button class="rstar" id="rstar${n}" onclick="selectStar(${n})">${n}</button>`
  ).join('');
  document.getElementById('ratingBackdrop').classList.add('open');
}

function selectStar(n) {
  if (!ratingCtx) return;
  ratingCtx.score = n;
  for (let i=1;i<=10;i++) {
    const el = document.getElementById('rstar'+i);
    if (el) el.classList.toggle('selected', i<=n);
  }
}

function closeRatingModal(e) {
  if (e && e.target !== document.getElementById('ratingBackdrop')) return;
  document.getElementById('ratingBackdrop').classList.remove('open');
}

async function submitRating() {
  if (!ratingCtx) return;
  const errEl = document.getElementById('ratingError');
  const okEl  = document.getElementById('ratingSuccess');
  errEl.style.display = 'none'; okEl.style.display = 'none';
  if (!ratingCtx.score) { errEl.textContent='Please select a rating first.'; errEl.style.display='block'; return; }
  const btn = document.getElementById('ratingSubmitBtn');
  btn.disabled = true; btn.textContent = 'Submitting…';
  try {
    const res  = await fetch('/rate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        course_id: ratingCtx.courseId, assignment_id: ratingCtx.assignmentId,
        student_id: ratingCtx.studentId, score: ratingCtx.score
      })
    });
    const json = await res.json();
    if (json.ok) {
      const r = json.rating;
      const key = `${ratingCtx.courseId}|${ratingCtx.assignmentId}|${ratingCtx.studentId}`;
      ratingsMap[key] = r;
      const avgEl = document.getElementById(ratingCtx.cid);
      if (avgEl) avgEl.innerHTML =
        `<span style="font-size:1.1rem;font-weight:800;color:#007acc">${r.avg}</span>`+
        `<span style="font-size:0.75rem;color:#aaa">/10</span> `+
        `<span style="font-size:0.72rem;color:#bbb">(${r.count} vote${r.count!==1?'s':''})</span>`;
      okEl.textContent = `✓ You rated this ${ratingCtx.score}/10. Thanks!`;
      okEl.style.display = 'block';
      btn.textContent = 'Submitted!';
      setTimeout(() => document.getElementById('ratingBackdrop').classList.remove('open'), 1400);
    } else {
      errEl.textContent = json.error || 'Failed to submit.';
      errEl.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Submit Rating';
    }
  } catch(e) {
    errEl.textContent = 'Network error — please try again.';
    errEl.style.display = 'block';
    btn.disabled = false; btn.textContent = 'Submit Rating';
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US',{month:'short',day:'numeric'});
}
function fmtName(full) {
  const parts = String(full).trim().split(/\s+/);
  if (parts.length < 2) return full;
  return parts[0][0].toUpperCase() + '. ' + parts.slice(1).join(' ');
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function ea(s) { return esc(s); }

// Close modals on Escape
document.addEventListener('keydown', e => {
  if (e.key==='Escape') {
    document.getElementById('modalBackdrop').classList.remove('open');
    document.getElementById('ratingBackdrop').classList.remove('open');
  }
});

loadAll();
</script>
</body>
</html>'''

# ── Announce page template ─────────────────────────────────────────────────────

ANNOUNCE_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Post Announcement – CSIT 212</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f5f7fa; font-family: "Segoe UI", Arial, sans-serif; min-height: 100vh;
    display: flex; align-items: flex-start; justify-content: center; padding: 48px 16px; }
  .card { background: #fff; border-radius: 14px; box-shadow: 0 4px 24px rgba(0,0,0,0.1);
    width: 100%; max-width: 640px; padding: 36px 40px; }
  h1 { font-size: 1.35rem; font-weight: 800; color: #007acc; margin-bottom: 6px; }
  .subtitle { font-size: 0.85rem; color: #999; margin-bottom: 28px; }
  .targets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
  .tag { background: #e8f4fd; color: #2b6cb0; border-radius: 20px; padding: 4px 12px;
    font-size: 0.78rem; font-weight: 700; }
  label { display: block; font-size: 0.78rem; font-weight: 700; color: #555;
    text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 6px; margin-top: 20px; }
  input, textarea { width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px;
    font-size: 0.95rem; font-family: inherit; outline: none; resize: vertical; color: #222; }
  input:focus, textarea:focus { border-color: #007acc; box-shadow: 0 0 0 3px rgba(0,122,204,0.12); }
  textarea { min-height: 160px; }
  .hint { font-size: 0.75rem; color: #aaa; margin-top: 5px; }
  .actions { margin-top: 28px; display: flex; gap: 12px; align-items: center; }
  .btn { padding: 11px 28px; border: none; border-radius: 8px; font-size: 0.9rem;
    font-weight: 700; cursor: pointer; transition: background 0.15s; }
  .btn-primary { background: #007acc; color: #fff; }
  .btn-primary:hover { background: #005fa3; }
  .btn-primary:disabled { background: #a0c8e8; cursor: not-allowed; }
  .btn-clear { background: #eee; color: #666; }
  .btn-clear:hover { background: #ddd; }
  .results { margin-top: 22px; border-radius: 10px; overflow: hidden; display: none; }
  .result-row { display: flex; align-items: center; gap: 10px; padding: 11px 16px;
    font-size: 0.875rem; border-bottom: 1px solid #f0f0f0; }
  .result-row:last-child { border-bottom: none; }
  .result-row.ok   { background: #f0fff4; }
  .result-row.fail { background: #fff5f5; }
  .icon { font-size: 1rem; }
  .result-label { font-weight: 700; flex: 1; }
  .result-info  { font-size: 0.78rem; color: #888; word-break: break-all; }
  .result-info a { color: #007acc; }

  .section-checks { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 4px; }
  .check-label { display: flex; align-items: center; gap: 6px; padding: 6px 12px;
    border: 1px solid #e2e8f0; border-radius: 20px; cursor: pointer;
    transition: all 0.12s; font-size: 0.82rem; font-weight: 600; color: #444; background: #f7fafc; }
  .check-label:hover { border-color: #007acc; background: #e8f4fd; color: #007acc; }
  .check-label input[type=checkbox] { accent-color: #007acc; cursor: pointer; width: 13px; height: 13px; }
  .check-label input[type=checkbox]:checked + span { color: #007acc; }
  .cid { display: none; }
</style>
</head>
<body>
<div class="card">
  <h1>📢 Post Announcement</h1>
  <p class="subtitle">Select which sections to post to, then write your announcement</p>

  <label style="margin-top:0">Post to Sections *</label>
  <div class="section-checks">
    <label class="check-label"><input type="checkbox" name="section" value="212615" checked><span>CSIT212_15SP26</span></label>
    <label class="check-label"><input type="checkbox" name="section" value="212604" checked><span>CSIT212_04SP26</span></label>
    <label class="check-label"><input type="checkbox" name="section" value="216874" checked><span>CSIT212_74SP26</span></label>
  </div>

  <label>Announcement Title *</label>
  <input type="text" id="title" placeholder="e.g. Final Project Due Date Reminder">
  <label>Message *</label>
  <textarea id="body" placeholder="Write your announcement here…"></textarea>
  <div class="hint">HTML is supported. Students will see this in Canvas under Announcements.</div>
  <div class="actions">
    <button class="btn btn-primary" id="submitBtn" onclick="postAnnouncement()">Post to All Sections</button>
    <button class="btn btn-clear" onclick="clearForm()">Clear</button>
  </div>
  <div class="results" id="results"></div>
</div>
<script>
async function postAnnouncement() {
  const title    = document.getElementById('title').value.trim();
  const body     = document.getElementById('body').value.trim();
  const selected = [...document.querySelectorAll('input[name=section]:checked')].map(c => c.value);
  if (!selected.length) { alert('Please select at least one section.'); return; }
  if (!title) { alert('Please enter a title.'); return; }
  if (!body)  { alert('Please write a message.'); return; }
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = 'Posting…';
  try {
    const res  = await fetch('/announce', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title, body, course_ids: selected})
    });
    const json = await res.json();
    const el   = document.getElementById('results');
    el.style.display = 'block';
    el.innerHTML = json.results.map(r => `
      <div class="result-row ${r.ok?'ok':'fail'}">
        <span class="icon">${r.ok?'✅':'❌'}</span>
        <span class="result-label">${r.label}</span>
        <span class="result-info">${r.ok
          ? '<a href="'+r.info+'" target="_blank">View in Canvas ↗</a>'
          : r.info}</span>
      </div>`).join('');
    btn.textContent = json.ok ? '✓ Posted!' : 'Partial failure';
    btn.disabled = false;
  } catch(e) {
    alert('Network error: ' + e.message);
    btn.disabled = false; btn.textContent = 'Post to All Sections';
  }
}
function clearForm() {
  document.getElementById('title').value = '';
  document.getElementById('body').value  = '';
  document.getElementById('results').style.display = 'none';
  document.getElementById('submitBtn').textContent = 'Post to All Sections';
  document.getElementById('submitBtn').disabled = false;
}
document.addEventListener('keydown', e => {
  if ((e.ctrlKey||e.metaKey) && e.key==='Enter') postAnnouncement();
});
</script>
</body>
</html>'''

# ── Startup ────────────────────────────────────────────────────────────────────

threading.Thread(target=keep_alive, daemon=True).start()


import task_engine

@app.route('/task', methods=['POST', 'OPTIONS'])
def task_endpoint():
    if request.method == 'OPTIONS':
        resp = app.response_class('')
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Canvas-Token, X-Canvas-Domain'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return resp
    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    token  = (data.get('token')  or request.headers.get('X-Canvas-Token') or '').strip()
    domain = (data.get('domain') or request.headers.get('X-Canvas-Domain') or '').strip().rstrip('/')
    course_id = data.get('course_id') or data.get('cid')
    dry      = bool(data.get('dry_run'))
    if not prompt:
        return jsonify({'ok': False, 'error': 'prompt required'}), 400
    plan = task_engine.plan_task(prompt)
    if not plan:
        return jsonify({'ok': False, 'error': 'Could not parse. Try: announce: TITLE | MESSAGE / publish all / shift dates by N / full credit on NAME / email all|failing|missing: SUBJECT | BODY / create assignment: NAME | due 5/22 | 20 pts'}), 200
    if dry:
        resp = jsonify({'ok': True, 'plan': plan, 'dry_run': True})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    if not token or not domain:
        return jsonify({'ok': False, 'error': 'token and domain required (send in request body or headers)'}), 400
    try:
        result = task_engine.execute(plan, token, domain, course_id)
        resp = jsonify({'ok': True, 'plan': plan, 'result': result})
    except Exception as e:
        resp = jsonify({'ok': False, 'plan': plan, 'error': str(e)})
        resp.status_code = 500
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

if __name__ == '__main__':
    print('\n  Canvas Final Project Server')
    print('  Local:  http://localhost:5050\n')
    app.run(host='0.0.0.0', port=5050, debug=False)
