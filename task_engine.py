"""Backend task engine.
Parses a natural-language task (regex/keyword for now; AI-ready hook),
then executes against Canvas using the caller's token/domain.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request


def _canvas(method, path, token, domain, body=None):
    url = domain.rstrip('/') + path
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json+canvas-string-ids'}
    data = None
    if body is not None and method in ('POST', 'PUT'):
        if isinstance(body, list):
            data = urllib.parse.urlencode(body, doseq=True).encode()
        elif isinstance(body, dict):
            data = urllib.parse.urlencode(list(body.items()), doseq=True).encode()
        else:
            data = body
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode('utf-8', 'ignore')


def parse_task(prompt):
    """Return a plan dict or None."""
    p = prompt.strip()

    m = re.match(r'^announce[:\s]+(.+?)\s*\|\s*(.+)$', p, re.IGNORECASE | re.DOTALL)
    if m:
        return {'type': 'announce', 'title': m.group(1).strip(), 'message': m.group(2).strip()}

    if re.match(r'^publish\s+all', p, re.IGNORECASE):
        return {'type': 'publish_all', 'publish': True}
    if re.match(r'^unpublish\s+all', p, re.IGNORECASE):
        return {'type': 'publish_all', 'publish': False}

    m = re.match(r'^shift\s+dates\s+by\s+(-?\d+)', p, re.IGNORECASE)
    if m:
        return {'type': 'shift_dates', 'days': int(m.group(1))}

    m = re.match(r'^full\s+credit\s+on\s+(.+)$', p, re.IGNORECASE)
    if m:
        return {'type': 'full_credit', 'name': m.group(1).strip()}

    m = re.match(r'^email\s+(all|failing|missing)[:\s]+(.+?)\s*\|\s*(.+)$', p, re.IGNORECASE | re.DOTALL)
    if m:
        return {'type': 'email', 'group': m.group(1).lower(),
                'subject': m.group(2).strip(), 'body': m.group(3).strip()}

    m = re.match(r'^create\s+assignment[:\s]+(.+)$', p, re.IGNORECASE | re.DOTALL)
    if m:
        rest = m.group(1)
        pts = 10
        d = re.search(r'(\d+)\s*pts?\b', rest, re.IGNORECASE)
        if d:
            pts = int(d.group(1))
            rest = rest.replace(d.group(0), '')
        due = None
        d = re.search(r'due\s+([0-9/\-]+(?:\s+\d{1,2}:\d{2}(?:\s*[ap]m)?)?)', rest, re.IGNORECASE)
        if d:
            due = d.group(1).strip()
            rest = rest.replace(d.group(0), '')
        parts = [seg.strip() for seg in rest.split('|') if seg.strip()]
        name = parts[0] if parts else 'New Assignment'
        return {'type': 'create_assignment', 'name': name, 'due': due, 'points': pts}

    return None


def execute(plan, token, domain, course_id):
    """Execute a parsed plan and return a result dict."""
    t = plan.get('type')
    if not course_id:
        raise ValueError('course_id required')

    if t == 'announce':
        body = [('title', plan['title']), ('message', plan['message']),
                ('is_announcement', 'true'), ('published', 'true')]
        out = _canvas('POST', f'/api/v1/courses/{course_id}/discussion_topics', token, domain, body)
        return {'created': out.get('id'), 'title': out.get('title')}

    if t == 'publish_all':
        asns = _canvas('GET', f'/api/v1/courses/{course_id}/assignments?per_page=100', token, domain) or []
        changed = 0
        for a in asns:
            if bool(a.get('published')) != plan['publish']:
                _canvas('PUT', f'/api/v1/courses/{course_id}/assignments/{a["id"]}',
                        token, domain, [('assignment[published]', 'true' if plan['publish'] else 'false')])
                changed += 1
        return {'total': len(asns), 'changed': changed, 'state': 'published' if plan['publish'] else 'unpublished'}

    if t == 'shift_dates':
        asns = _canvas('GET', f'/api/v1/courses/{course_id}/assignments?per_page=100', token, domain) or []
        from datetime import datetime, timedelta, timezone
        days = int(plan['days'])
        changed = 0
        for a in asns:
            due = a.get('due_at')
            if not due:
                continue
            dt = datetime.fromisoformat(due.replace('Z', '+00:00'))
            new = dt + timedelta(days=days)
            _canvas('PUT', f'/api/v1/courses/{course_id}/assignments/{a["id"]}',
                    token, domain, [('assignment[due_at]', new.isoformat().replace('+00:00', 'Z'))])
            changed += 1
        return {'shifted_by_days': days, 'changed': changed}

    if t == 'full_credit':
        asns = _canvas('GET', f'/api/v1/courses/{course_id}/assignments?per_page=100', token, domain) or []
        match = None
        for a in asns:
            if plan['name'].lower() in (a.get('name') or '').lower():
                match = a
                break
        if not match:
            raise ValueError(f"No assignment found matching '{plan['name']}'")
        subs = _canvas('GET', f'/api/v1/courses/{course_id}/assignments/{match["id"]}/submissions?per_page=100', token, domain) or []
        max_pts = match.get('points_possible') or 100
        graded = 0
        for s in subs:
            if not s.get('submitted_at'):
                continue
            _canvas('PUT', f'/api/v1/courses/{course_id}/assignments/{match["id"]}/submissions/{s["user_id"]}',
                    token, domain, [('submission[posted_grade]', str(max_pts))])
            graded += 1
        return {'assignment': match['name'], 'graded': graded, 'max_points': max_pts}

    if t == 'email':
        # Recipient resolution: 'all' → course_<id>_students; 'failing' → students with current_score < 70
        if plan['group'] == 'all':
            recipients = [f'course_{course_id}_students']
        else:
            enrolls = _canvas('GET', f'/api/v1/courses/{course_id}/enrollments?type[]=StudentEnrollment&per_page=100&include[]=user&include[]=current_grading_period_scores', token, domain) or []
            if plan['group'] == 'failing':
                ids = [str(e['user_id']) for e in enrolls if float((e.get('grades') or {}).get('current_score') or 0) and float(e['grades']['current_score']) < 70]
            else:  # missing
                ids = [str(e['user_id']) for e in enrolls if not e.get('last_activity_at')]
            if not ids:
                return {'sent_to': 0, 'note': f'No students match group={plan["group"]}.'}
            recipients = ids
        body = [('subject', plan['subject']), ('body', plan['body']), ('context_code', f'course_{course_id}'),
                ('group_conversation', 'false'), ('mode', 'async')]
        for r in recipients:
            body.append(('recipients[]', r))
        out = _canvas('POST', '/api/v1/conversations', token, domain, body)
        return {'sent_to_recipients': len(recipients), 'message_count_estimate': out if isinstance(out, dict) else None}

    if t == 'create_assignment':
        from datetime import datetime
        body = [('assignment[name]', plan['name']),
                ('assignment[points_possible]', str(plan.get('points') or 10)),
                ('assignment[published]', 'true'),
                ('assignment[grading_type]', 'points'),
                ('assignment[submission_types][]', 'online_upload')]
        if plan.get('due'):
            # Accept either 5/22 or 5/22/26 etc.
            d = plan['due']
            m = re.match(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?:\s+(\d{1,2}):(\d{2})\s*([ap]m)?)?', d, re.IGNORECASE)
            if m:
                mo, da = int(m.group(1)), int(m.group(2))
                yr = m.group(3)
                yr = int(yr) if yr else datetime.utcnow().year
                if yr < 100:
                    yr += 2000
                hh = int(m.group(4) or 23)
                mm = int(m.group(5) or 59)
                if m.group(6) and m.group(6).lower() == 'pm' and hh < 12:
                    hh += 12
                iso = f'{yr:04d}-{mo:02d}-{da:02d}T{hh:02d}:{mm:02d}:00Z'
                body.append(('assignment[due_at]', iso))
        out = _canvas('POST', f'/api/v1/courses/{course_id}/assignments', token, domain, body)
        return {'created': out.get('id'), 'name': out.get('name'), 'due_at': out.get('due_at')}

    raise ValueError(f'Unsupported plan type: {t}')


# AI-ready hook: if ANTHROPIC_API_KEY is set, try LLM parsing first.
def llm_parse(prompt):
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return None
    # Stub for future implementation. When the API key lands, call /v1/messages here
    # with a system prompt that returns the same plan dict shape.
    return None


def plan_task(prompt):
    return llm_parse(prompt) or parse_task(prompt)
