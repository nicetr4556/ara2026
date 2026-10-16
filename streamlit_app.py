# -*- coding: utf-8 -*-
"""
안건제안서 생성 보조도구
경상남도 학생의회 · <서식 2> 안건제안서(교육지원청용)

학생이 '문제 상황'과 '해결책'을 쓰면 생성형 AI가 새 양식의 칸을 채운 초안을 만들고,
학생이 확인·수정한 뒤 한글(.hwpx) 양식 그대로 내려받는다.

[선생님 설정] Streamlit Cloud > 앱 Settings > Secrets 에 아래 중 하나만 있으면 된다.
    GEMINI_API_KEY = "AIza..."         # Google Gemini (무료 키 가능)
    OPENAI_API_KEY = "sk-..."          # OpenAI
    ANTHROPIC_API_KEY = "sk-ant-..."   # Anthropic Claude
  · 이름이 달라도 키 모양(AIza… / sk-… / sk-ant-…)을 보고 자동으로 찾는다.
  · 선택: AI_MODEL = "모델이름"  (기본 모델 대신 쓸 모델)
필요 패키지: streamlit, requests (requests는 streamlit을 설치하면 함께 설치된다)
"""

import base64
import html
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime

import requests
import streamlit as st

# ─────────────────────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────────────────────
REGIONS = [
    "창원", "진주", "통영", "사천", "김해", "밀양", "거제", "양산", "의령",
    "함안", "창녕", "고성", "남해", "하동", "산청", "함양", "거창", "합천",
]
SCOPES = ["AI가 판단", "교육지원청", "경상남도교육청"]
MAX_CALLS_PER_SESSION = 25  # 한 학생(브라우저 탭)당 AI 호출 상한

DEFAULT_MODELS = {
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"],
    "openai": ["gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"],
    "anthropic": ["claude-sonnet-5-5", "claude-sonnet-4-5", "claude-haiku-4-5"],
}

LIST_FIELDS = [
    "problems", "limits", "necessity", "plans", "schedule",
    "effect_student", "effect_school", "effect_community",
]
TEXT_FIELDS = ["title", "proposal", "dept_main", "dept_coop"]
INLINE_FIELDS = {"dept_main", "dept_coop"}  # "○ 항목 : 내용" 한 줄로 쓰는 칸

# 양식 순서 (미리보기 · 텍스트 · 한글 파일이 모두 이 순서를 따른다)
STRUCTURE = [
    ("제안 배경", [("현황 및 문제점", "problems"), ("기존 정책의 한계", "limits"), ("제안의 필요성", "necessity")]),
    ("주요 내용", [("제안 사항", "proposal"), ("세부 추진 방안", "plans"), ("추진 일정 및 소요 예산", "schedule")]),
    ("기대 효과", [("학생 측면", "effect_student"), ("학교 및 교육 현장 측면", "effect_school"),
                ("정책·지역사회 측면", "effect_community")]),
    ("소관 부서", [("제안 처리 부서", "dept_main"), ("협조 요청 부서·기관", "dept_coop")]),
]

PLACEHOLDER_RE = re.compile(r"【[^【】]*】")

SPEECH_TIMES = {"1분": 1, "2분": 2, "3분": 3}
SPEECH_CHARS_PER_MIN = 350      # 프롬프트 분량 기준(공백 포함 글자 수)
SPEECH_SYLLABLES_PER_MIN = 280  # 예상 발표 시간 계산 기준(학생이 또박또박 읽는 속도)

EXAMPLE = {
    "ap_in_title": "",
    "ap_in_problem": (
        "학교 가는 길에 인도가 없거나 좁은 곳이 많아서 차도 옆으로 걸어야 해요. "
        "아침 등교 시간에 승용차와 트럭이 빠르게 지나가서 무섭고, 비 오는 날에는 우산 때문에 더 위험해요. "
        "가로등도 적어서 겨울에 해가 빨리 지면 하굣길이 어두워요."
    ),
    "ap_in_solution": (
        "학교 주변 통학로에 안전한 인도와 보행 울타리를 만들고, 어두운 곳에 가로등이나 바닥 조명을 설치했으면 좋겠어요. "
        "학생들이 직접 위험한 곳을 조사해서 '통학로 안전 지도'를 만들고, 교육지원청과 군청에 알리는 활동도 하고 싶어요."
    ),
    "ap_in_evidence": "",
    "ap_in_opinion": "학생자치회 회의에서 통학로 문제를 안건으로 이야기했어요.",
    "ap_in_current": "",
    "ap_in_budget": "",
}

SETUP_HELP = """
**AI 키 연결 방법 (선생님용)**
1. [share.streamlit.io](https://share.streamlit.io) 에서 이 앱의 **⋮ → Settings → Secrets** 를 엽니다.
2. 아래 중 **한 줄**만 넣고 저장합니다.
```toml
GEMINI_API_KEY = "AIza로 시작하는 키"
# OPENAI_API_KEY = "sk-로 시작하는 키"
# ANTHROPIC_API_KEY = "sk-ant-로 시작하는 키"
```
3. 저장하면 앱이 자동으로 다시 시작됩니다. Gemini 키는 [Google AI Studio](https://aistudio.google.com/apikey) 에서 무료로 만들 수 있어요.
"""


# ─────────────────────────────────────────────────────────────
# AI 연결
# ─────────────────────────────────────────────────────────────
class AIError(Exception):
    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


def _secret_items():
    """st.secrets(중첩 포함)와 일부 환경변수를 (경로, 값) 목록으로 펼친다."""
    found = []
    root = {}
    try:
        root = st.secrets.to_dict()
    except Exception:
        try:
            root = {k: st.secrets[k] for k in st.secrets.keys()}
        except Exception:
            root = {}

    def walk(node, path):
        if isinstance(node, str):
            found.append(("/".join(path).lower(), node.strip()))
        elif isinstance(node, dict) or hasattr(node, "items"):
            try:
                pairs = list(node.items())
            except Exception:
                return
            for k, v in pairs:
                walk(v, path + [str(k)])

    walk(root, [])
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER", "AI_MODEL"):
        if os.environ.get(name):
            found.append((name.lower(), os.environ[name].strip()))
    return found


def detect_provider():
    """(제공자, 키, 지정 모델) — 키가 없으면 (None, None, None)."""
    items = _secret_items()

    def getv(name):
        for p, v in items:
            if p.split("/")[-1] == name:
                return v
        return ""

    forced = getv("ai_provider").lower()
    forced = {"google": "gemini", "claude": "anthropic", "gpt": "openai"}.get(forced, forced)
    keys = {"gemini": "", "openai": "", "anthropic": ""}
    for path, val in items:
        last = path.split("/")[-1]
        if not val or " " in val or "model" in last or "provider" in last:
            continue
        if val.startswith("sk-ant-"):
            prov = "anthropic"
        elif val.startswith("AIza"):
            prov = "gemini"
        elif val.startswith("sk-"):
            prov = "openai"
        elif "key" in last and ("gemini" in path or "google" in path):
            prov = "gemini"
        elif "key" in last and "openai" in path:
            prov = "openai"
        elif "key" in last and ("anthropic" in path or "claude" in path):
            prov = "anthropic"
        else:
            continue
        if not keys[prov]:
            keys[prov] = val
    order = ([forced] if forced in keys else []) + ["gemini", "openai", "anthropic"]
    for p in order:
        if keys.get(p):
            return p, keys[p], (getv("ai_model") or getv(p + "_model"))
    return None, None, None


def _post(url, headers, body):
    try:
        r = requests.post(url, headers=headers, json=body, timeout=(10, 180))
    except requests.Timeout:
        raise AIError("AI 응답이 너무 늦어요. 잠시 후 다시 눌러 주세요.", "rate")
    except requests.RequestException:
        raise AIError("AI 서버에 연결하지 못했어요. 잠시 후 다시 눌러 주세요.", "network")
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            raise AIError("AI 답변을 읽지 못했어요. 다시 눌러 주세요.", "parse")
    text = (r.text or "")[:800]
    low = text.lower()
    if r.status_code == 404 or "model_not_found" in low or ("model" in low and "not found" in low):
        raise AIError("model not found", "notfound")
    if r.status_code in (429, 500, 502, 503, 504, 529) or "overloaded" in low:
        raise AIError("지금 AI를 쓰는 사람이 많아요. 30초쯤 뒤에 다시 눌러 주세요.", "rate")
    if r.status_code in (401, 403) or "api key not valid" in low or "api_key_invalid" in low or "invalid x-api-key" in low:
        raise AIError("AI 키가 올바르지 않아요. 선생님께 알려 주세요.", "auth")
    raise AIError(f"AI 요청이 실패했어요(오류 {r.status_code}). 잠시 후 다시 눌러 주세요.", "other")


def _call_gemini(key, model, system, user):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    data = _post(url, {"x-goog-api-key": key, "Content-Type": "application/json"}, body)
    cands = data.get("candidates") or []
    if not cands:
        raise AIError("AI가 이 내용에는 답하지 않았어요. 표현을 바꿔서 다시 써 주세요.", "safety")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise AIError("AI가 빈 답변을 보냈어요. 다시 눌러 주세요.", "parse")
    return text


def _call_openai(key, model, system, user):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    data = _post("https://api.openai.com/v1/chat/completions",
                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError("AI 답변을 읽지 못했어요. 다시 눌러 주세요.", "parse")


def _call_anthropic(key, model, system, user):
    body = {"model": model, "max_tokens": 4000, "system": system,
            "messages": [{"role": "user", "content": user}]}
    data = _post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, body)
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


CALLERS = {"gemini": _call_gemini, "openai": _call_openai, "anthropic": _call_anthropic}


def parse_json_loose(text):
    t = (text or "").strip()
    tries = [t]
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        tries.append(m.group(1))
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        tries.append(t[i:j + 1])
    for cand in tries:
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    raise AIError("AI 답변을 읽지 못했어요. 다시 눌러 주세요.", "parse")


def ask_ai(system, user):
    """AI에게 묻고 JSON(dict)을 돌려준다. 모델이 없거나 붐비면 다음 모델로 넘어간다."""
    prov, key, forced_model = detect_provider()
    if not prov:
        raise AIError("아직 AI가 연결되지 않았어요. 선생님께 알려 주세요.", "nokey")
    models = ([forced_model] if forced_model else []) + [m for m in DEFAULT_MODELS[prov] if m != forced_model]
    caller = CALLERS[prov]
    last = None
    for attempt in range(2):
        for m in list(models):
            try:
                return parse_json_loose(caller(key, m, system, user)), prov, m
            except AIError as e:
                last = e
                if e.kind == "notfound":
                    models.remove(m)
                    continue
                if e.kind in ("rate", "parse", "network"):
                    continue
                raise
        if not models:
            break
        time.sleep(5)
    if last is None or last.kind == "notfound":
        raise AIError("사용할 수 있는 AI 모델을 찾지 못했어요. 선생님께 알려 주세요.", "notfound")
    raise last


# ─────────────────────────────────────────────────────────────
# 프롬프트
# ─────────────────────────────────────────────────────────────
JSON_SHAPE = """{
  "status": "ok",
  "message": "",
  "title": "안건명",
  "problems": ["현황 및 문제점 항목", "..."],
  "limits": ["기존 정책의 한계 항목"],
  "necessity": ["제안의 필요성 항목"],
  "proposal": "제안 사항(1~2문장)",
  "plans": ["(주체) 세부 추진 방안 항목"],
  "schedule": ["추진 일정 및 소요 예산 항목"],
  "effect_student": ["학생 측면 기대 효과"],
  "effect_school": ["학교 및 교육 현장 측면 기대 효과"],
  "effect_community": ["정책·지역사회 측면 기대 효과"],
  "dept_main": "제안 처리 부서",
  "dept_coop": "협조 요청 부서·기관 (없으면 '해당 없음')",
  "tips": ["학생에게 주는 조언"]
}"""


def plan_year():
    now = datetime.now()
    return now.year + 1 if now.month >= 9 else now.year


def system_prompt():
    y = plan_year()
    return f"""너는 경상남도 학생의회 학생들이 교육지원청에 내는 「안건제안서(정책 건의 보고서)」를 쓰도록 돕는 글쓰기 도우미다.
학생이 적은 '문제 상황'과 '해결책'을 바탕으로 〈서식 2〉 안건제안서(교육지원청용)의 각 칸을 채운 초안을 만든다.

[양식 항목과 작성 기준]
- 안건명(title): 제안의 핵심이 드러나는 15~25자 명사형 제목. 예) '학생참여예산제 도입을 통한 학생자치 활성화', '안전한 통학로 조성을 위한 보행 환경 개선'
- 제안 배경
  · 현황 및 문제점(problems): 학교·지역의 현재 실태를 설문 결과, 통계, 사례 등 근거와 함께 작성. 2~4개.
  · 기존 정책의 한계(limits): 현행 제도·사업으로 해결되지 않는 부분. 1~3개.
  · 제안의 필요성(necessity): 제안 이유와 학생 의견 수렴 과정(설문, 토론 등). 2~3개.
- 주요 내용
  · 제안 사항(proposal): 핵심 제안을 1~2문장으로 요약.
  · 세부 추진 방안(plans): 누가·무엇을·어떻게 실행할지 항목별로 구체적으로. 3~5개. 각 항목은 '(주체) 내용' 형식. 예) '(합천교육지원청) 학생들이 조사한 통학로 위험 지점 자료를 군청에 전달하고 보행로 정비 협의'
  · 추진 일정 및 소요 예산(schedule): 해당하는 경우 작성(예산 산출 근거 포함). 1~3개.
- 기대 효과: 학생 측면(effect_student), 학교 및 교육 현장 측면(effect_school), 정책·지역사회 측면(effect_community) 각 1~2개.
- 소관 부서: 제안 처리 부서(dept_main), 협조 요청 부서·기관(dept_coop).

[반드시 지킬 원칙]
1. 문체는 공문서 개조식이다. 각 항목은 '~함', '~임', '~필요함', '~예정임', '~기대됨'처럼 명사형으로 끝내고 1~2문장(80자 안팎)으로 짧게 쓴다. 항목 앞에 '-', '•', 번호를 붙이지 않는다.
2. 학생의 생각이 주인공이다. 학생이 쓴 문제와 해결책의 핵심은 바꾸지 말고 논리적이고 구체적으로 다듬는다. 학생이 말하지 않은 거창한 사업을 새로 지어내지 않는다.
3. 사실을 지어내지 않는다. 학생이 주지 않은 숫자·통계·설문 결과·사고 건수·예산 금액·기사·실제 정책이나 사업 이름은 절대 만들지 않는다. 근거가 필요한 자리에는 학생이 조사해서 채울 빈칸을 【 】로 남긴다. 예) 【설문 결과: 우리 학교 학생 ○명 중 ○명(○%)이 등굣길이 위험하다고 응답】, 【예상 예산: ○○원(산출 근거)】. 빈칸은 전체 3~6개, 꼭 필요한 곳에만 둔다.
4. 학생이 근거 자료·의견 수렴 과정·현재 제도·일정과 비용을 적었다면 그 내용을 정확히 그대로 활용하고, 그 부분은 빈칸으로 만들지 않는다.
5. 교육지원청 단위 사항(지역 학교 지원, 지역 기관과의 협력)과 경상남도교육청 단위 정책(도 전체 제도·예산)을 구분해서 쓴다.
6. 소관 부서: 학생 지역의 교육지원청은 '○○교육지원청'(예: 합천교육지원청)으로 쓴다. 과 이름은 확실하지 않으므로 끝에 '(확인 필요)'를 붙인다. 예) '합천교육지원청 교육지원과(확인 필요)'. 도교육청 단위 정책이면 '경상남도교육청 ○○과(확인 필요)'처럼 쓴다. 협조 기관(학교, 군청·시청, 경찰서 등)은 실제로 필요한 경우만 쓰고, 없으면 '해당 없음'이라고 쓴다.
7. 일정은 {y}년 기준의 대략적인 시기(예: '{y}년 3~4월')로 쓴다.
8. tips에는 제안서를 더 설득력 있게 만들 구체적인 방법(무엇을 조사하거나 보완하면 좋은지)을 2~3개, 학생에게 말하듯 친근한 존댓말('~해 보세요')로 쓴다.
9. 입력이 욕설·특정인 비방·다른 사람의 개인정보·장난·제안서와 무관한 내용이면 status를 "retry"로 하고, message에 학생이 다시 쓸 수 있도록 친절하게 안내한다(다른 칸은 비워 둔다).

[출력 형식] 설명 없이 아래 구조의 JSON 하나만 출력한다.
{JSON_SHAPE}"""


def _or_none(s):
    s = (s or "").strip()
    return s if s else "없음"


def user_prompt(inp):
    region = inp.get("region") or ""
    office = f"{region}교육지원청" if region else "(지역 미입력 — '○○교육지원청'으로 쓸 것)"
    title = inp.get("title", "").strip() or "없음 — 알맞은 안건명을 지어 줄 것"
    return f"""[발의 지역] {region or '미입력'} → 담당 교육지원청: {office}
[소속] {_or_none(inp.get('school'))}
[학생이 정한 안건명] {title}
[제안 단위] {inp.get('scope') or 'AI가 판단'}

[문제 상황]
{inp.get('problem', '').strip()}

[해결책]
{inp.get('solution', '').strip()}

[근거 자료(설문·통계·사례)]
{_or_none(inp.get('evidence'))}

[학생 의견 수렴 과정]
{_or_none(inp.get('opinion'))}

[현재 있는 제도나 노력]
{_or_none(inp.get('current'))}

[예상 일정·비용]
{_or_none(inp.get('budget'))}

위 내용으로 안건제안서 초안을 JSON으로 작성해 줘."""


def revision_prompt(doc, request_text, inp):
    cur = {k: doc.get(k) for k in TEXT_FIELDS + LIST_FIELDS}
    first = user_prompt(inp) if inp else ""
    return f"""[학생이 처음 쓴 내용]
{first}

[현재 안건제안서 JSON]
{json.dumps(cur, ensure_ascii=False, indent=1)}

[학생의 수정 요청]
{request_text.strip()}

위 수정 요청을 반영해 같은 구조의 JSON 전체를 다시 출력해 줘.
요청과 관련 없는 칸은 그대로 두고, 위 원칙(특히 사실을 지어내지 않기, 【 】 빈칸 유지)은 그대로 지켜 줘.
학생이 직접 채운 숫자나 사실은 바꾸지 마. tips의 첫 항목에는 이번에 무엇을 고쳤는지 한 문장으로 알려 줘."""


SPEECH_SHAPE = """{
  "parts": [
    {"label": "인사와 자기소개", "text": "..."},
    {"label": "안건 소개", "text": "..."},
    {"label": "제안 배경", "text": "..."},
    {"label": "제안 내용", "text": "..."},
    {"label": "기대 효과", "text": "..."},
    {"label": "마무리", "text": "..."}
  ],
  "qa": [{"q": "예상 질문", "a": "답변"}]
}"""


def speech_system_prompt():
    return f"""너는 경상남도 학생의회 학생의원이 정례회에서 자신이 낸 안건을 발표할 때 읽을 「안건 발표문」을 써 주는 도우미다.

[반드시 지킬 원칙]
1. 학생이 소리 내어 읽는 글이다. '~습니다', '~입니다'로 끝나는 발표체로 쓰고, 한 문장은 40자 안팎으로 짧게, 어려운 한자어 대신 쉬운 낱말을 쓴다. 제안서의 개조식 표현('~함', '~임')을 그대로 옮기지 않는다.
2. 제안서에 있는 내용만 쓴다. 제안서에 없는 숫자·통계·사례·사업 이름은 절대 새로 만들지 않는다. 제안서에 【 】 빈칸이 있으면 발표문에도 같은 【 】 빈칸을 그대로 남긴다.
3. 발표문은 6부분(인사와 자기소개 / 안건 소개 / 제안 배경 / 제안 내용 / 기대 효과 / 마무리)으로 나눈다.
   · 인사와 자기소개는 '존경하는 의장님, 그리고 학생의원 여러분, 안녕하십니까. 〔소속〕 〔이름〕입니다.'로 시작한다. 〔소속〕과 〔이름〕은 글자 그대로 둔다.
   · 안건 소개에서는 안건명을 정확히 말한다.
   · 제안 배경에서는 듣는 사람이 공감하도록 문제 상황을 생생하게 말하고 근거를 든다.
   · 제안 내용에서는 무엇을, 누가, 어떻게 하자는 것인지 핵심만 말한다.
   · 마무리에서는 안건에 동의해 달라고 부탁하고 감사 인사로 끝낸다.
4. 요청한 분량(공백 포함 글자 수)에 맞춘다.
5. qa에는 의원이나 선생님이 물어볼 만한 예상 질문 3개와, 학생이 제안서 내용을 근거로 답할 수 있는 2~3문장의 답변(발표체)을 쓴다. 예상 질문을 만들지 말라고 하면 빈 목록으로 둔다.

[출력 형식] 설명 없이 아래 구조의 JSON 하나만 출력한다.
{SPEECH_SHAPE}"""


def speech_user_prompt(doc, meta, minutes, with_qa):
    proposal = render_text(doc, {"region": meta.get("region", ""), "school": "〔소속〕", "name": "〔이름〕"})
    return f"""[안건제안서]
{proposal}

[발표 시간] {minutes}분 — 공백 포함 약 {minutes * SPEECH_CHARS_PER_MIN}자
[예상 질문] {'3개 만들기' if with_qa else '만들지 않음(qa는 빈 목록)'}

위 안건제안서로 정례회 발표문을 JSON으로 작성해 줘."""


def normalize_speech(raw):
    raw = raw if isinstance(raw, dict) else {}
    parts = []
    src = raw.get("parts")
    if isinstance(src, list):
        for p in src:
            if isinstance(p, dict):
                label, text = str(p.get("label", "") or "").strip(), str(p.get("text", "") or "").strip()
            else:
                label, text = "", str(p or "").strip()
            text = _BAD_XML_CHARS.sub("", text)
            if text:
                parts.append({"label": label, "text": text})
    elif isinstance(raw.get("speech"), str):
        parts = [{"label": "", "text": t.strip()} for t in re.split(r"\n\s*\n", raw["speech"]) if t.strip()]
    qa = []
    for item in raw.get("qa") or []:
        if isinstance(item, dict):
            q = str(item.get("q") or item.get("question") or "").strip()
            a = str(item.get("a") or item.get("answer") or "").strip()
            if q and a:
                qa.append({"q": q, "a": a})
    return {"parts": parts, "qa": qa}


def fill_tokens(text, meta):
    return (text.replace("〔소속〕", (meta.get("school") or "").strip() or "○○학교")
                .replace("〔이름〕", (meta.get("name") or "").strip() or "○○○"))


def speech_seconds(text):
    return round(len(re.findall(r"[가-힣A-Za-z0-9]", text)) / SPEECH_SYLLABLES_PER_MIN * 60)


def fmt_duration(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}분 {s:02d}초" if m else f"{s}초"


def doc_sig(doc):
    return json.dumps({k: doc.get(k) for k in TEXT_FIELDS + LIST_FIELDS}, ensure_ascii=False, sort_keys=True)


# ─────────────────────────────────────────────────────────────
# 데이터 정리
# ─────────────────────────────────────────────────────────────
_BULLET_RE = re.compile(r"^\s*(?:[-–•·∙▪◦*○]|\d+[.)])\s*")
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_item(s):
    s = _BAD_XML_CHARS.sub("", str(s or "")).replace("\t", " ").strip()
    return _BULLET_RE.sub("", s).strip()


def normalize_doc(raw):
    raw = raw if isinstance(raw, dict) else {}
    d = {}
    for f in TEXT_FIELDS:
        v = raw.get(f, "")
        if isinstance(v, list):
            v = " ".join(str(x) for x in v)
        d[f] = " ".join(clean_item(x) for x in str(v or "").splitlines() if clean_item(x))
    for f in LIST_FIELDS + ["tips"]:
        v = raw.get(f, [])
        if isinstance(v, str):
            v = v.splitlines()
        if not isinstance(v, list):
            v = [v]
        d[f] = [c for c in (clean_item(x) for x in v) if c]
    d["status"] = str(raw.get("status", "ok") or "ok").strip().lower()
    d["message"] = str(raw.get("message", "") or "").strip()
    return d


def has_content(doc):
    return bool(doc.get("title") or doc.get("proposal") or any(doc.get(f) for f in LIST_FIELDS))


def doc_items(doc, field):
    v = doc.get(field)
    if isinstance(v, list):
        return [x for x in v if x]
    return [x for x in [clean_item(v)] if x]


def count_placeholders(doc):
    n = 0
    for f in TEXT_FIELDS + LIST_FIELDS:
        for t in doc_items(doc, f):
            n += len(PLACEHOLDER_RE.findall(t))
    return n


def split_placeholders(text):
    out, pos = [], 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos:m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


# ─────────────────────────────────────────────────────────────
# 미리보기 / 텍스트
# ─────────────────────────────────────────────────────────────
CSS = """
<style>
.ap-paper{background:#fff!important;color:#111!important;border:1px solid #d5d5d5;border-radius:4px;
 box-shadow:0 2px 12px rgba(0,0,0,.07);padding:26px 24px;margin:6px auto 4px;max-width:780px;
 font-family:"HCR Batang","함초롬바탕","Batang","바탕","Noto Serif KR","Nanum Myeongjo",serif;line-height:1.75;font-size:15px}
.ap-paper *{color:#111}
.ap-formno{font-weight:700;margin:0 0 10px;font-size:15px}
.ap-titlebox{border-top:7px solid #d9d9d9;border-bottom:7px solid #d9d9d9;padding:10px 6px;margin:0 0 16px;text-align:center;
 font-family:"Malgun Gothic","맑은 고딕","Apple SD Gothic Neo",sans-serif;font-weight:800;font-size:22px;line-height:1.4}
.ap-paper table.ap-t{width:100%!important;border-collapse:collapse!important;table-layout:fixed;margin:0!important;display:table!important}
.ap-paper .ap-t th,.ap-paper .ap-t td{border:1px solid #333!important;padding:4px 8px!important;font-size:14px;background:#fff!important;
 vertical-align:middle;text-align:center;font-weight:400}
.ap-paper .ap-t td.ap-lab{color:#1f3a93}
.ap-paper .ap-t td.ap-val{text-align:left}
.ap-paper .ap-t th.ap-band{background:#d9d9d9!important;font-weight:700;font-size:15px}
.ap-paper .ap-t td.ap-body{text-align:left!important;vertical-align:top;padding:12px 16px!important;font-size:15px}
.ap-sec{font-weight:800;margin:16px 0 2px}
.ap-sec.ap-first{margin-top:0}
.ap-sub{font-weight:700;margin:6px 0 2px 14px}
.ap-inline{font-weight:400}
.ap-paper ul.ap-items{margin:0 0 4px 34px!important;padding:0!important;list-style:none!important}
.ap-paper ul.ap-items li{margin:0 0 2px!important;padding:0 0 0 14px!important;text-indent:-14px;list-style:none!important}
.ap-paper ul.ap-items li::before{content:"- "}
.ap-paper mark.ap-ph{background:#fff1a6!important;color:#8a4a00!important;padding:0 2px;border-radius:3px}
.ap-empty{color:#9a9a9a!important;margin-left:34px;font-size:13px}
.ap-speech{background:#fffdf6!important;color:#1a1a1a!important;border:1px solid #e8dcbc;border-left:6px solid #e0b84f;border-radius:6px;
 padding:20px 24px;margin:6px auto 8px;max-width:780px;font-size:17px;line-height:1.95;
 font-family:"Malgun Gothic","맑은 고딕","Apple SD Gothic Neo","Noto Sans KR",sans-serif}
.ap-speech *{color:#1a1a1a}
.ap-sp-meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;margin:0 0 6px;padding-bottom:8px;border-bottom:1px dashed #e3d6b3}
.ap-sp-meta b{color:#7a5a12!important}
.ap-sp-meta span{color:#6b6b6b!important}
.ap-sp-label{font-size:12px;font-weight:700;color:#9a7420!important;margin:14px 0 0;letter-spacing:.02em}
.ap-sp-p{margin:0 0 4px!important}
.ap-speech mark.ap-ph{background:#fff1a6!important;color:#8a4a00!important;padding:0 2px;border-radius:3px}
@media (max-width:640px){.ap-speech{padding:14px 12px;font-size:16px}}
@media (max-width:640px){.ap-paper{padding:14px 10px;font-size:14px}.ap-titlebox{font-size:18px}
 .ap-paper .ap-t td.ap-body{padding:10px 8px!important;font-size:14px}.ap-paper ul.ap-items{margin-left:18px!important}}
</style>
"""


def _rich(text):
    out = []
    for is_ph, s in split_placeholders(text):
        out.append(f'<mark class="ap-ph">{html.escape(s)}</mark>' if is_ph else html.escape(s))
    return "".join(out)


def render_preview_html(doc, meta):
    e = html.escape
    body = []
    for si, (sec, subs) in enumerate(STRUCTURE):
        body.append(f'<div class="ap-sec{" ap-first" if si == 0 else ""}">■ {e(sec)}</div>')
        for label, f in subs:
            items = doc_items(doc, f)
            if f in INLINE_FIELDS:
                val = _rich(" ".join(items)) if items else '<span class="ap-empty">(비어 있음)</span>'
                body.append(f'<div class="ap-sub">○ {e(label)} <span class="ap-inline">: {val}</span></div>')
            else:
                body.append(f'<div class="ap-sub">○ {e(label)}</div>')
                if items:
                    body.append('<ul class="ap-items">' + "".join(f"<li>{_rich(i)}</li>" for i in items) + "</ul>")
                else:
                    body.append('<div class="ap-empty">(비어 있음)</div>')
    title = doc.get("title") or "(안건명)"
    return (
        '<div class="ap-paper">'
        '<div class="ap-formno">&lt;서식 2&gt; 안건제안서(교육지원청용)</div>'
        f'<div class="ap-titlebox">{_rich(title)}</div>'
        '<table class="ap-t"><colgroup><col style="width:24%"><col style="width:18%"><col style="width:34%"><col style="width:24%"></colgroup>'
        '<tr><th>안건번호</th><th colspan="2">발 의 자</th><th>가결여부</th></tr>'
        f'<tr><td rowspan="3" style="font-size:12px">기재하지 않음</td><td class="ap-lab">지역</td><td class="ap-val">{e(meta.get("region", ""))}</td><td rowspan="3">○</td></tr>'
        f'<tr><td class="ap-lab">소속</td><td class="ap-val">{e(meta.get("school", ""))}</td></tr>'
        f'<tr><td class="ap-lab">이름</td><td class="ap-val">{e(meta.get("name", ""))}</td></tr>'
        '<tr><th colspan="4" class="ap-band">안건제안서 (정책 건의 보고서)</th></tr>'
        f'<tr><td colspan="4" class="ap-body">{"".join(body)}</td></tr>'
        "</table></div>"
    )


def render_text(doc, meta, newline="\n"):
    lines = [
        "<서식 2> 안건제안서(교육지원청용)", "",
        f"안건명: {doc.get('title', '')}",
        f"발의자: 지역 {meta.get('region', '')} / 소속 {meta.get('school', '')} / 이름 {meta.get('name', '')}",
        "", "안건제안서 (정책 건의 보고서)",
    ]
    for sec, subs in STRUCTURE:
        lines += ["", f"■ {sec}"]
        for label, f in subs:
            items = doc_items(doc, f)
            if f in INLINE_FIELDS:
                lines.append(f"  ○ {label} : {' '.join(items)}")
            else:
                lines.append(f"  ○ {label}")
                lines += [f"    - {i}" for i in items]
    return newline.join(lines)


def render_speech_html(parts, text, meta, seconds):
    e = html.escape
    if parts:
        blocks = [(p.get("label", ""), fill_tokens(p["text"], meta)) for p in parts]
    else:
        blocks = [("", para) for para in re.split(r"\n\s*\n", text) if para.strip()]
    body = "".join(
        (f'<div class="ap-sp-label">{e(label)}</div>' if label else "")
        + '<p class="ap-sp-p">' + "<br>".join(_rich(line) for line in t.strip().splitlines()) + "</p>"
        for label, t in blocks
    )
    nchars = len(re.sub(r"\s+", " ", text).strip())
    head = (f'<div class="ap-sp-meta"><b>🎤 안건 발표문</b><span>예상 발표 시간 약 {fmt_duration(seconds)}</span>'
            f"<span>{nchars}자</span></div>")
    return f'<div class="ap-speech">{head}{body}</div>'


def speech_text_export(doc, text, qa, meta):
    lines = [
        "〈안건 발표문〉",
        f"안건명: {doc.get('title', '')}",
        f"발표자: {(meta.get('school') or '').strip()} {(meta.get('name') or '').strip()}".rstrip(),
        f"예상 발표 시간: 약 {fmt_duration(speech_seconds(text))}",
        "", text.strip(),
    ]
    if qa:
        lines += ["", "[예상 질문과 답변]"]
        for i, item in enumerate(qa, 1):
            lines += [f"Q{i}. {item['q']}", f"A. {fill_tokens(item['a'], meta)}", ""]
    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────
# 한글(.hwpx) 만들기 — 원본 양식 파일에 내용만 채워 넣는다
# ─────────────────────────────────────────────────────────────
SAMPLE_TITLE = "학생참여예산제 도입을 통한 학생자치 활성화"


def _xml(text):
    return html.escape(_BAD_XML_CHARS.sub("", text), quote=False)


def _run(cid, text=None):
    if not text:
        return f'<hp:run charPrIDRef="{cid}"/>'
    return f'<hp:run charPrIDRef="{cid}"><hp:t>{_xml(text)}</hp:t></hp:run>'


def _para(pid, *runs):
    return (f'<hp:p id="2147483648" paraPrIDRef="{pid}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
            + "".join(runs) + "</hp:p>")


def _rich_runs(text, base, red):
    return [_run(red if is_ph else base, s) for is_ph, s in split_placeholders(text) if s]


def _add_char_styles(header):
    """제목용(검정·굵게) 글자 모양과 빈칸 강조용(빨강) 글자 모양을 추가하고 번호를 돌려준다."""
    ids = [int(x) for x in re.findall(r'<hh:charPr id="(\d+)"', header)]
    title_src = re.search(r'<hh:charPr id="14".*?</hh:charPr>', header, re.S)
    body_src = re.search(r'<hh:charPr id="22".*?</hh:charPr>', header, re.S)
    cnt = re.search(r'<hh:charProperties itemCnt="(\d+)"', header)
    if not (ids and title_src and body_src and cnt):
        return header, 14, 22
    tid, rid = max(ids) + 1, max(ids) + 2
    t = re.sub(r'id="14"', f'id="{tid}"', title_src.group(0), count=1)
    t = re.sub(r'textColor="#[0-9A-Fa-f]{6}"', 'textColor="#000000"', t, count=1).replace("<hh:italic/>", "")
    r = re.sub(r'id="22"', f'id="{rid}"', body_src.group(0), count=1)
    r = re.sub(r'textColor="#[0-9A-Fa-f]{6}"', 'textColor="#D60000"', r, count=1)
    header = header.replace("</hh:charProperties>", t + r + "</hh:charProperties>", 1)
    header = header.replace(cnt.group(0), f'<hh:charProperties itemCnt="{int(cnt.group(1)) + 2}"', 1)
    return header, tid, rid


def _body_paragraphs(doc, red):
    P = []
    for si, (sec, subs) in enumerate(STRUCTURE):
        if si > 0:
            P.append(_para(28, _run(22)))
        P.append(_para(27, _run(20, " "), _run(21, f" ■ {sec}")))
        for li, (label, f) in enumerate(subs):
            items = doc_items(doc, f)
            if f in INLINE_FIELDS:
                runs = _rich_runs(" ".join(items), 22, red) if items else []
                P.append(_para(28, _run(21, f"    ○ {label}"), _run(22, " : "), *runs))
                continue
            if li > 0:
                P.append(_para(28, _run(22)))
            P.append(_para(28, _run(21, f"    ○ {label}")))
            for it in (items or [""]):
                P.append(_para(28, _run(22, "       - "), *_rich_runs(it, 22, red)))
    return "".join(P)


def _set_info_cell(sec, row, text):
    """발의자 표의 값 칸(2열, row행)에 글자를 넣는다."""
    addr = f'<hp:cellAddr colAddr="2" rowAddr="{row}"/>'
    for m in re.finditer(r"<hp:tc\b.*?</hp:tc>", sec, re.S):
        block = m.group(0)
        if addr not in block:
            continue
        new_block, n = re.subn(r'<hp:run charPrIDRef="(\d+)"\s*/>',
                               lambda mm: _run(mm.group(1), text), block, count=1)
        if n == 0:
            new_block = re.sub(r'<hp:run charPrIDRef="(\d+)">.*?</hp:run>',
                               lambda mm: _run(mm.group(1), text), block, count=1, flags=re.S)
        return sec[:m.start()] + new_block + sec[m.end():]
    return sec


def fill_section(sec, doc, meta, title_cid, red_cid):
    title = " " + (doc.get("title") or "")
    if SAMPLE_TITLE in sec:
        sec = re.sub(r'<hp:run charPrIDRef="\d+"><hp:t>\s*' + re.escape(SAMPLE_TITLE) + r"</hp:t></hp:run>",
                     lambda m: _run(title_cid, title), sec, count=1)
    for row, key in ((1, "region"), (2, "school"), (3, "name")):
        val = (meta.get(key) or "").strip()
        if val:
            sec = _set_info_cell(sec, row, val)
    idx = sec.find("■ 제안 배경")
    if idx != -1:
        tc = sec.rfind("<hp:tc ", 0, idx)
        sub_open = sec.find("<hp:subList", tc)
        start = sec.find(">", sub_open) + 1
        end = sec.find("</hp:subList>", start)
        if tc != -1 and sub_open != -1 and end != -1:
            sec = sec[:start] + _body_paragraphs(doc, red_cid) + sec[end:]
    return sec


def build_hwpx(doc, meta):
    src = zipfile.ZipFile(io.BytesIO(base64.b64decode(TEMPLATE_B64)))
    header, title_cid, red_cid = _add_char_styles(src.read("Contents/header.xml").decode("utf-8"))
    sec = fill_section(src.read("Contents/section0.xml").decode("utf-8"), doc, meta, title_cid, red_cid)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "Contents/section0.xml":
                data = sec.encode("utf-8")
            elif info.filename == "Contents/header.xml":
                data = header.encode("utf-8")
            elif info.filename == "Preview/PrvText.txt":
                data = render_text(doc, meta, newline="\r\n").encode("utf-8")
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            z.writestr(zi, data)
    return out.getvalue()


def safe_filename(*parts):
    name = "_".join(p for p in parts if p)
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "안건제안서"


# ─────────────────────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────────────────────
def _ek(ver, field):
    return f"ap_e{ver}_{field}"


def current_doc(doc0, ver):
    """'직접 고치기' 칸에 학생이 고친 내용을 반영한 현재 제안서."""
    ss = st.session_state
    d = dict(doc0)
    for f in TEXT_FIELDS:
        if _ek(ver, f) in ss:
            d[f] = " ".join(clean_item(x) for x in str(ss[_ek(ver, f)]).splitlines() if clean_item(x))
    for f in LIST_FIELDS:
        if _ek(ver, f) in ss:
            d[f] = [c for c in (clean_item(x) for x in str(ss[_ek(ver, f)]).splitlines()) if c]
    return d


def fill_example():
    for k, v in EXAMPLE.items():
        st.session_state[k] = v


def show_ai_error(e):
    if e.kind in ("nokey", "auth", "notfound"):
        st.error(str(e))
        with st.expander("선생님용 설정 방법"):
            st.markdown(SETUP_HELP)
    elif e.kind == "rate":
        st.warning(str(e))
    else:
        st.error(str(e))


def render_editor(doc0, ver):
    st.caption("한 줄에 한 항목씩 써요. 고친 내용은 미리보기와 내려받는 파일에 바로 반영돼요. 【 】 빈칸은 조사한 내용으로 바꿔 주세요.")
    st.text_input("안건명", value=doc0.get("title", ""), key=_ek(ver, "title"))
    for sec, subs in STRUCTURE:
        st.markdown(f"**■ {sec}**")
        for label, f in subs:
            if f in LIST_FIELDS:
                items = doc0.get(f, [])
                height = max(100, min(300, 34 * max(1, len(items)) + 60))
                st.text_area(f"○ {label}", value="\n".join(items), key=_ek(ver, f), height=height)
            elif f == "proposal":
                st.text_area(f"○ {label}", value=doc0.get(f, ""), key=_ek(ver, f), height=100)
            else:
                st.text_input(f"○ {label}", value=doc0.get(f, ""), key=_ek(ver, f))


def run_revision(doc, request_text):
    ss = st.session_state
    if ss.ap_calls >= MAX_CALLS_PER_SESSION:
        st.error("오늘은 AI를 너무 많이 썼어요. 직접 고치기 탭에서 마무리해 주세요.")
        return
    with st.spinner("AI가 제안서를 고쳐 쓰고 있어요… (보통 10~40초)"):
        try:
            raw, prov, model = ask_ai(system_prompt(), revision_prompt(doc, request_text, ss.ap_inputs))
        except AIError as e:
            show_ai_error(e)
            return
    ss.ap_calls += 1
    new = normalize_doc(raw)
    if new["status"] == "retry":
        st.warning(new["message"] or "요청을 조금 더 구체적으로 써 주세요.")
        return
    if not has_content(new):
        st.error("AI 답변이 비어 있어요. 다시 한 번 눌러 주세요.")
        return
    ss.ap_doc, ss.ap_engine = new, f"{prov} · {model}"
    ss.ap_ver += 1
    ss.ap_flash = "AI가 요청대로 고쳐 썼어요. 바뀐 부분을 확인해 보세요."
    st.rerun()


def render_speech_section(doc, meta, n_ph):
    ss = st.session_state
    st.divider()
    st.subheader("6. 발표문 만들기")
    st.caption("정례회에서 이 안건을 발표할 때 읽을 발표문을 만들어요. 제안서를 다 고친 뒤에 만들면 고친 내용이 그대로 들어가요.")
    if n_ph:
        st.caption("※ 【 】 빈칸을 채우지 않고 만들면 발표문에도 빈칸으로 남아요.")
    c1, c2 = st.columns([1.3, 1])
    with c1:
        length = st.radio("발표 시간", list(SPEECH_TIMES), index=1, horizontal=True, key="ap_sp_len")
    with c2:
        with_qa = st.checkbox("예상 질문과 답변도 만들기", value=True, key="ap_sp_qa")

    sp = ss.ap_speech
    if st.button("🎤 발표문 다시 만들기" if sp else "🎤 발표문 만들기", type="primary", key="ap_sp_go"):
        if ss.ap_calls >= MAX_CALLS_PER_SESSION:
            st.error("오늘은 AI를 너무 많이 썼어요. 만든 발표문을 직접 고쳐서 마무리해 주세요.")
        else:
            minutes = SPEECH_TIMES[length]
            raw = None
            with st.spinner("AI가 발표문을 쓰고 있어요… (보통 10~40초)"):
                try:
                    raw, _, _ = ask_ai(speech_system_prompt(), speech_user_prompt(doc, meta, minutes, with_qa))
                except AIError as e:
                    show_ai_error(e)
            if raw is not None:
                ss.ap_calls += 1
                new = normalize_speech(raw)
                if not new["parts"]:
                    st.error("발표문이 비어 있어요. 다시 한 번 눌러 주세요.")
                else:
                    new["sig"], new["minutes"] = doc_sig(doc), minutes
                    ss.ap_speech = sp = new
                    ss.ap_sp_ver += 1
    if not sp:
        return

    ver = ss.ap_sp_ver
    key = f"ap_sp_text_{ver}"
    gen_text = fill_tokens("\n\n".join(p["text"] for p in sp["parts"]), meta)
    text = str(ss[key]) if key in ss else gen_text
    if sp.get("sig") != doc_sig(doc):
        st.info("발표문을 만든 뒤에 제안서가 바뀌었어요. 바뀐 내용을 넣으려면 '🎤 발표문 다시 만들기'를 누르세요.")
    labeled = text.strip() == gen_text.strip()
    st.markdown(render_speech_html(sp["parts"] if labeled else None, text, meta, speech_seconds(text)),
                unsafe_allow_html=True)
    if PLACEHOLDER_RE.search(text):
        st.warning("발표문에 【 】 빈칸이 있어요. 발표하기 전에 조사한 내용으로 바꿔 주세요.")
    with st.expander("✏️ 발표문 직접 고치기"):
        st.caption("단락 사이는 한 줄 띄워 주세요. 고친 내용은 위 발표문과 내려받는 파일에 바로 반영돼요.")
        st.text_area("발표문", value=gen_text, key=key, height=380, label_visibility="collapsed")
    if sp["qa"]:
        st.markdown("**🙋 예상 질문과 답변** — 질문을 누르면 답변 예시가 보여요.")
        for i, item in enumerate(sp["qa"], 1):
            with st.expander(f"Q{i}. {item['q']}"):
                st.write(fill_tokens(item["a"], meta))
    st.download_button("⬇️ 발표문(.txt) 내려받기", data=speech_text_export(doc, text, sp["qa"], meta).encode("utf-8"),
                       file_name=safe_filename("발표문", meta["school"], meta["name"]) + ".txt",
                       mime="text/plain", key="ap_sp_dl")


def main():
    try:
        st.set_page_config(page_title="안건제안서 생성 보조도구", page_icon="📝", layout="centered")
    except Exception:
        pass
    st.markdown(CSS, unsafe_allow_html=True)

    ss = st.session_state
    for k, v in (("ap_doc", None), ("ap_ver", 0), ("ap_calls", 0), ("ap_inputs", None),
                 ("ap_flash", None), ("ap_engine", ""), ("ap_speech", None), ("ap_sp_ver", 0)):
        if k not in ss:
            ss[k] = v

    st.title("📝 안건제안서 생성 보조도구")
    st.caption("경상남도 학생의회 · 〈서식 2〉 안건제안서(교육지원청용)")
    st.markdown(
        "**문제 상황**과 **해결책**을 쓰면 AI가 새 안건제안서 양식에 맞춰 초안을 만들어 줘요. "
        "만든 뒤에는 꼭 읽어 보고 내 생각에 맞게 고친 다음, **한글 파일(.hwpx)** 로 내려받아 제출하세요. "
        "제안서 아래에서 **정례회 발표문**도 바로 만들 수 있어요."
    )
    with st.expander("처음이라면 읽어 보세요 · 사용 방법"):
        st.markdown(
            "1. **발의자 정보**(지역·소속·이름)를 써요.\n"
            "2. **문제 상황**과 **해결책**을 내 말로 편하게 써요. 설문 결과나 사례가 있으면 추가 정보에 적어요.\n"
            "3. **✨ AI로 제안서 만들기**를 눌러요. 10~40초 정도 걸려요.\n"
            "4. 미리보기를 읽고, **✏️ 직접 고치기**나 **🤖 AI에게 고쳐 달라고 하기**로 다듬어요.\n"
            "5. **⬇️ 한글 파일 내려받기**를 눌러 담당 선생님께 제출해요.\n"
            "6. 맨 아래 **🎤 발표문 만들기**를 누르면 정례회에서 읽을 발표문과 예상 질문까지 만들어 줘요.\n\n"
            "💡 AI는 숫자나 통계를 지어내지 않아요. 근거가 필요한 곳은 노란색 **【 】 빈칸**으로 남겨 두니, "
            "직접 설문하거나 조사한 내용으로 채워 주세요."
        )

    # 1. 발의자 정보
    st.subheader("1. 발의자 정보")
    c1, c2, c3 = st.columns([1, 1.5, 1.2])
    with c1:
        region = st.selectbox("지역", REGIONS, index=None, placeholder="선택", key="ap_in_region")
    with c2:
        school = st.text_input("소속 (학교)", key="ap_in_school", max_chars=40, placeholder="예: ○○초등학교")
    with c3:
        name = st.text_input("이름", key="ap_in_name", max_chars=40, placeholder="예: 홍길동 외 2명")

    # 2. 문제 상황과 해결책
    st.subheader("2. 문제 상황과 해결책")
    st.button("💡 예시로 채워 보기", on_click=fill_example, help="통학로 안전 예시가 채워져요. 내 내용으로 바꿔 쓰면 돼요.")
    title_in = st.text_input("안건 제목 (비워 두면 AI가 지어 줘요)", key="ap_in_title", max_chars=60,
                             placeholder="예: 안전한 통학로 조성을 위한 보행 환경 개선")
    problem = st.text_area(
        "😟 문제 상황 *", key="ap_in_problem", height=150, max_chars=1500,
        placeholder="무엇이 불편하거나 문제인가요? 언제, 어디서, 누가 겪고 있나요?\n"
                    "예: 학교 앞 길에 인도가 좁아서 등교할 때 차도 옆으로 걸어야 해요.")
    solution = st.text_area(
        "💡 해결책 *", key="ap_in_solution", height=150, max_chars=1500,
        placeholder="어떻게 바꾸면 좋을까요? 누가(교육지원청, 학교, 군청, 학생자치회 등) 무엇을 해 주면 될까요?")
    with st.expander("➕ 더 설득력 있는 제안서를 위한 추가 정보 (선택)"):
        st.caption("아는 만큼만 써도 돼요. 여기에 쓴 숫자와 사실은 AI가 그대로 넣고, 비워 두면 【 】 빈칸으로 남겨요.")
        evidence = st.text_area("📊 근거 자료 (설문 결과, 통계, 사례, 기사 등)", key="ap_in_evidence", height=100,
                                max_chars=1200, placeholder="예: 5~6학년 48명에게 설문했더니 35명(73%)이 '등굣길이 위험하다'고 답함")
        opinion = st.text_area("🗣️ 학생 의견을 모은 과정 (설문, 토론, 회의 등)", key="ap_in_opinion", height=80,
                               max_chars=800, placeholder="예: 학생자치회 회의에서 토론하고, 전교생 설문을 함")
        current = st.text_area("📌 지금 있는 제도나 노력 (알고 있다면)", key="ap_in_current", height=80,
                               max_chars=800, placeholder="예: 등교 시간에 녹색어머니회가 교통 지도를 하지만 하교 시간에는 없음")
        budget = st.text_area("🗓️ 예상 일정·비용 (알고 있다면)", key="ap_in_budget", height=80,
                              max_chars=800, placeholder="예: 내년 1학기, 바닥 조명 10개(개당 약 30만 원)")
        scope = st.radio("제안 단위", SCOPES, key="ap_in_scope", horizontal=True,
                         help="교육지원청: 우리 지역 학교들에 해당하는 일 / 경상남도교육청: 경남 전체에 해당하는 정책")

    # 3. 만들기
    st.subheader("3. AI로 제안서 만들기")
    go = st.button("✨ AI로 제안서 만들기", type="primary")
    if go:
        short = [n for n, v in (("문제 상황", problem), ("해결책", solution)) if len((v or "").strip()) < 10]
        if short:
            st.warning(f"먼저 {'·'.join(short)}을(를) 10자 이상 써 주세요.")
        elif ss.ap_calls >= MAX_CALLS_PER_SESSION:
            st.error("오늘은 AI를 너무 많이 썼어요. 지금 만든 제안서를 직접 고쳐서 마무리해 주세요.")
        else:
            inputs = {"region": region or "", "school": school, "title": title_in, "scope": scope,
                      "problem": problem, "solution": solution, "evidence": evidence,
                      "opinion": opinion, "current": current, "budget": budget}
            with st.spinner("AI가 제안서를 쓰고 있어요… (보통 10~40초 걸려요)"):
                try:
                    raw, prov, model = ask_ai(system_prompt(), user_prompt(inputs))
                except AIError as e:
                    raw = None
                    show_ai_error(e)
            if raw is not None:
                ss.ap_calls += 1
                doc = normalize_doc(raw)
                if doc["status"] == "retry":
                    st.warning(doc["message"] or "내용을 조금 더 구체적으로 써 주세요.")
                elif not has_content(doc):
                    st.error("AI 답변이 비어 있어요. 다시 한 번 눌러 주세요.")
                else:
                    if title_in.strip():
                        doc["title"] = title_in.strip()
                    ss.ap_doc, ss.ap_inputs, ss.ap_engine = doc, inputs, f"{prov} · {model}"
                    ss.ap_ver += 1
                    ss.ap_speech = None  # 새 제안서 → 이전 발표문은 지운다
                    ss.ap_flash = "제안서 초안이 만들어졌어요! 아래에서 읽어 보고 고쳐 보세요."

    # 4~5. 결과
    doc0 = ss.ap_doc
    if doc0:
        st.divider()
        st.subheader("4. 확인하고 고치기")
        if ss.ap_flash:
            st.success(ss.ap_flash)
            ss.ap_flash = None
        ver = ss.ap_ver
        doc = current_doc(doc0, ver)
        meta = {"region": region or "", "school": (school or "").strip(), "name": (name or "").strip()}

        if doc0.get("tips"):
            st.info("**🤖 AI의 조언**\n\n" + "\n".join(f"- {t}" for t in doc0["tips"]))
        n_ph = count_placeholders(doc)
        if n_ph:
            st.warning(f"노란색 【 】 빈칸이 **{n_ph}곳** 남아 있어요. 직접 조사한 내용으로 채워 주세요. "
                       "('✏️ 직접 고치기' 탭에서 바꿀 수 있어요)")

        tab1, tab2, tab3 = st.tabs(["📄 미리보기", "✏️ 직접 고치기", "🤖 AI에게 고쳐 달라고 하기"])
        with tab1:
            st.markdown(render_preview_html(doc, meta), unsafe_allow_html=True)
        with tab2:
            render_editor(doc0, ver)
        with tab3:
            st.caption("어떻게 고치고 싶은지 쓰면 AI가 제안서를 다시 다듬어 줘요. 직접 고친 내용도 함께 반영돼요.")
            presets = [
                ("세부 추진 방안 더 구체적으로", "세부 추진 방안을 누가·무엇을·어떻게가 더 잘 드러나게 구체적으로 고쳐 줘."),
                ("문장을 더 짧고 간결하게", "모든 항목의 문장을 더 짧고 간결한 개조식으로 고쳐 줘."),
                ("기대 효과 보강하기", "기대 효과를 학생·학교·지역사회 측면에서 더 설득력 있게 보강해 줘."),
            ]
            pc = st.columns(len(presets))
            chosen = None
            for i, (label, text) in enumerate(presets):
                with pc[i]:
                    if st.button(label, key=f"ap_preset_{ver}_{i}"):
                        chosen = text
            req = st.text_area("직접 요청하기", key=f"ap_req_{ver}", height=90, max_chars=500,
                               placeholder="예: 제안의 필요성에 우리 학교 학생자치회에서 토론한 내용을 넣어 줘")
            if st.button("🤖 이렇게 고쳐 줘", key=f"ap_revise_{ver}"):
                if len((req or "").strip()) < 3:
                    st.warning("어떻게 고치고 싶은지 먼저 써 주세요.")
                else:
                    chosen = req
            if chosen:
                run_revision(doc, chosen)

        st.subheader("5. 내려받기")
        if not all(meta.values()):
            st.caption("※ 위 1번의 지역·소속·이름을 채우면 파일의 발의자 칸에도 들어가요.")
        base = safe_filename("안건제안서", meta["school"], meta["name"])
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ 한글 파일(.hwpx) 내려받기", data=build_hwpx(doc, meta),
                               file_name=base + ".hwpx", mime="application/octet-stream", type="primary")
        with d2:
            st.download_button("⬇️ 텍스트(.txt) 내려받기", data=render_text(doc, meta).encode("utf-8"),
                               file_name=base + ".txt", mime="text/plain")
        with st.expander("📋 텍스트로 보기 (복사해서 붙여넣기)"):
            try:
                st.code(render_text(doc, meta), language=None, wrap_lines=True)
            except TypeError:
                st.code(render_text(doc, meta), language=None)

        render_speech_section(doc, meta, n_ph)

    with st.expander("⚙️ 선생님용 · AI 연결 상태"):
        prov, _, model = detect_provider()
        if prov:
            st.write(f"연결된 AI: **{prov}**" + (f" (지정 모델: {model})" if model else ""))
            if ss.ap_engine:
                st.write(f"마지막으로 답한 모델: {ss.ap_engine}")
        else:
            st.write("연결된 AI 키가 없어요.")
            st.markdown(SETUP_HELP)


# 원본 양식(〈서식 2〉 안건제안서.hwpx)을 그대로 담아 둔 데이터 — 수정하지 마세요.
TEMPLATE_B64 = (
    "UEsDBBQAAAAAAAAAIQCC8EFHEwAAABMAAAAIAAAAbWltZXR5cGVhcHBsaWNhdGlvbi9od3AremlwUEsDBBQAAAAAAAAAIQA8PNp2"
    "NQEAADUBAAALAAAAdmVyc2lvbi54bWw8P3htbCB2ZXJzaW9uPSIxLjAiIGVuY29kaW5nPSJVVEYtOCIgc3RhbmRhbG9uZT0ieWVz"
    "IiA/PjxodjpIQ0ZWZXJzaW9uIHhtbG5zOmh2PSJodHRwOi8vd3d3LmhhbmNvbS5jby5rci9od3BtbC8yMDExL3ZlcnNpb24iIHRh"
    "Z2V0QXBwbGljYXRpb249IldPUkRQUk9DRVNTT1IiIG1ham9yPSI1IiBtaW5vcj0iMSIgbWljcm89IjEiIGJ1aWxkTnVtYmVyPSIw"
    "IiBvcz0iMSIgeG1sVmVyc2lvbj0iMS41IiBhcHBsaWNhdGlvbj0iSGFuY29tIE9mZmljZSBIYW5ndWwiIGFwcFZlcnNpb249IjEy"
    "LCAwLCAwLCA1MzUgV0lOMzJMRVdpbmRvd3NfMTAiLz5QSwMEFAAEAAgAAAAhAOXS/bChFQAAqEcBABMAAABDb250ZW50cy9oZWFk"
    "ZXIueG1s7V1fb9tIkv8qhPKy92DLVBw7FsazkGUpdkaxDf/ZbF4moKWWxDFFckkqjvdpFosDFriHuwUywOJuFtjBAbfZwQAX3M5D"
    "gNv7QmPPd7iq7iZF8Y9IOhIl0h0/pEmxu6u6u35dXV1d/dkv34406Q2xbNXQdyvy+kZFInrX6Kn6YLdycd5ee1qRbEfRe4pm6GS3"
    "ckPsivTLzz8bDutDovQkyK7b9aGyWxk6jlmvVq+vr9eHChQxWu8a61dWdXhtjrRqbUOWq4ppVtwcZqocpmIpA0sxh5N88kaKnFvV"
    "cE47RT65apOuA23h1ddNlatrWMTLMkyVBZtvkiUdcUPVdgzrxss2SlXTSLEdYq2ZymBCo9mPz2p3h2SksC4bmn23tt6kKcyxpa0b"
    "1qDa61aJRkZEd+yqvC5X3W+NQPlqz+zTDLWNje0q/Dr50oAh1B0qlhNPkDeEtqqG97nXCNfmWFcdLCJVCQfX5gV834Tv3SKIOb6c"
    "yhskF8Y8G+hdQ++rIBljS68biq3adV0ZEbvudOuGSfSe0R1jY9T9X9epVPlk7AmIFOk2deBYrlBRuiQDVT8ajyTsI3wr9Q3D0Q2H"
    "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"
    "94LHUbc9BSpc1V1TmVfXTIP5b8P0C1BLAwQUAAAAAAAAACEAW1xJpIFyAACBcgAAFAAAAFByZXZpZXcvUHJ2SW1hZ2UucG5niVBO"
    "Rw0KGgoAAAANSUhEUgAAAtQAAAQACAIAAABOMFmkAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7D"
    "AcdvqGQAAHIWSURBVHhe7d3bles6DqDhiqsCcjyOYjJoR3BC6DUvfusMTgQzS6IEUwBIUTfaKP/fy7YpkKJBSeSWL/Xz/wAAADr6"
    "0QUAAABXYvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEB"
    "AAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6"
    "YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEB"
    "AAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6"
    "YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEB"
    "AAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6"
    "YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6YvEBAAC6+uOLj5+Z3lC2o0oHO3q1o8rWWpuC"
    "Rb1WZdMmshehI9asVrRbV6tYLVUkphJcKgeADxT1atV+qa1cr0u2VpH4vNamFlqo9ktsH1arqMjGKkn7LnKlWm7hDqr90u5WVSrK"
    "JhVgS9QmXVqtYrdWgkvlAPBpQl6q3IvsvotyqZZb6MpbUK21N2K5dW2h7NEqVcm5kZUq7ia3MEmb3K223JZU1IPtVltSoiLdivUY"
    "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"
    "n2VTila6c/CM/ve//nIhGT7ROtdYOaMLB/5s/v7N9LjQmwusn/ST1wnl5XGRxqypbUM51Ox3mGHE4uNijUd005n4+l9MKWh9bytX"
    "qi1WL1XZ/GPWL+7ao9RQw+s6jLVH5ujRsSkJpQ7POgz+Fo3dOemMXnfeGb3/oDc2HAAr/W7JozckG3rA2uM9WHxcq/GIXvwvySzu"
    "/9/wpfjsP5LFttb3dt6V6tilirWHz99fQ3dWQ1YDzvM6mksfBByP5ylm7VDsMPhbNHbnpDN63Xln9IlHyIazYK3fr6acPC7TmDe1"
    "oQeDf/7XOK44D4uPSzUf0e3nSuVkPaWRVocuVTvXHg12vrKz1h6XyCayVaUeb8nh7ixONu2r1OHZpsaO9btF3zN63UevPY53akMe"
    "K28tNWgeV5yGxceVthzRlb+bNau9dbvlND3jonDoUsXaY4toa4/mHtc+1DDb1PGj/V7X94xed940f+JBv2nM1ne3nsfDadw0rjgJ"
    "i48Lbb4yOH8Mof3vm7ef8s39KTt0qfq4tcfSepe6apzJR6Ueb8nhWVmcvudiGm89nkebOn5Kv6v6ntHrNven6MTZd9OYte0uvbti"
    "83hSGs989WjG4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHTF4gMAAHQVbPHxfwEA"
    "iExPbF+JxQcAAP3oie0rsfgAAKAfPbF9JRYfAAD0oye2r8TiAwCAfvTE9pVYfAAA0I+e2L4Siw8AAPrRE9tXCrb4AAAA0bH4AAAA"
    "XbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4AAAAXbH4"
    "AAAAXbH4AAAAXbH4AAAAXbH4AAAAXQVbfPwAABCWntW+VbBEMHJXIKtAdJzFITBMIlgiGLkrkFUgOs7iEBgmESwR3Ufu+dQlRRtC"
    "NwZfrXtWcdj7D6C3dwALn3sWPzccrBtCY/rcYeouWCJ6j9zzcf/9+b0/dLnj+bjfWmOn4J+bG9z9/OudVRz2fNx+h6Nt/6EytNB6"
    "uDqeB+vjbB97Flcvdsq2q6h3DD6fj8fjkV9DxzZ/b4/958qZPnaY+guWiMtH7nH7VSfJ49Z22gyqscM5cLu/ToFi8PP+mz6Y9Pt7"
    "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"
    "7qJzr1DHEIhgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "YOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "YOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "YOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "YOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "YOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABB"
    "MYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJYOQAAEExhYlgiWDkAABBMYWJYIlg5AAAQTGFiWCJ"
    "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"
    "NfuqlOjQNbp+QXskgPfibBXBEvEJI1fvg7vVLdxKGqm05m5yC0V96yo9YY500JKOHqmA/KkolQsb0FKyakeVkhObEle0CeAKnK0i"
    "WCI+YeTqfdCTakaH7lVvSu91puNm7ia3sCfd+4wOzaiA1fi/4RteI/A3cLaKYIn4hJGr96G+9RSn72Ixt2d0XAR/4CVs9SUvE/gD"
    "OFtFsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBE"
    "MHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICg"
    "mMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBE"
    "MHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICgmMJEsEQwcgCAoJjCRLBEMHIAgKCYwkSwRDByAICg"
    "mMJEsEQwcgCAoJjCRLBE9B+55yP31Jv3eN5vv6Pbanvz3tfitMc97SC5b6j/fMydu7dXUl5tNOx4zsamXgJAQP2nsI8VLBEdR+75"
    "uN9+f37GSXH0uL8KdHCTeU6+3R7Px+3n5+fnN5vgn8OCYSxcTPvPcRlxe7xKGj1fy6bn1v7a3jWYdjjVSa/Qa2IMW3Zp1/4AIJiO"
    "U9inC5aIXiP3vA/rAG82LG9xTSsAiZ6n2bs73abGf27TcietecabF22Lj+c93ejI3Ici51ZGtod5R6+uti0Gpq7d58XYsLhK3R+f"
    "Da/FNvF8Oi+9bX8AEFuvKSyAYInoM3LjJFqeCtO02roeSBPr3Ny8dnFm4GzrYvWw7c7HfP9hurWgdp8HPsaVyrh4GHeTxTUvBqbV"
    "kkRmKyWz+JjXJsObQeM9ntdyqHl/ABBYnykshGCJ6DFy40y4Mtu3xEyW83PL4mPZ7pY7HwvT+0ZN7xOpRUT7YmBe20yfLpk+6TFs"
    "UYsPuamzeD5tb98fAMTVYwoLIlgirh+56Z2LtWmweUmgJt22xcfi3ZDWPb3IsiPdZBDjR0+dV6bvX2xYDKRI6Z6583G7j+/73OwL"
    "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"
    "xRQmgiWCkQMABMUUJoIlIu7I/Xh0EADg7+KyL4IlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIl"
    "gpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIl"
    "gpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIl"
    "gpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIl"
    "gpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIl"
    "gpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAExRQmgiWCkQMABMUUJoIlgpEDAATFFCaCJYKRAwAE"
    "xRQmgiXiBwCAmPSU9sXIBQAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrF"
    "BwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA"
    "6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrF"
    "BwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA"
    "6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrF"
    "BwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA"
    "6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrF"
    "BwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA"
    "6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6IrFBwAA6Or/A8z6iIa/mzyHAAAAAElFTkSuQmCCUEsDBBQABAAIAAAAIQAn"
    "lsLdEwEAAGMDAAAWAAAATUVUQS1JTkYvY29udGFpbmVyLnJkZrWT3U6EMBCFX6XpXsMAJkbIwl5IiJfGnweoZQSy0JJOkeXtLeDF"
    "rtlodPWy7cyZL+dMt7tD17I3NNRolfLQDzhDJXXZqCrlz0+Fd8MZWaFK0WqFKZ+QONtlW1O+Jg95wVy7osSdUl5b2ycA4zj645Wv"
    "TQVhHMcQRBBFnqvwaFJWHDxFG74K5EjSNL11s9ksKF70YFPuXhUFSS3oXhj7McLdnIyohcPsfKn9vYF67LsWoiC8hg6tgH5fbfgi"
    "aZD0YKQjv9XKorIENYoSje/IOWRbmKuOQL4kO6exNtipx08Dj/z4FvZuYSqaFn/M9M9uEco5n+Ayv05U/sSxx5Xrt5bNqV+YWa7l"
    "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"
)

main()
