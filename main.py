import json
import os
import hashlib
import time
from typing import Any, Dict, List, Optional

import requests
import functions_framework
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.cloud import storage


# ---------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "ascn-win-visit-3287-sbx")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL_NAME = os.environ.get("VERTEX_MODEL_NAME", "gemini-2.5-flash")

# Trello configuration
TRELLO_API_KEY = os.environ.get("TRELLO_API_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
TRELLO_LIST_ID = os.environ.get("TRELLO_LIST_ID")

# Optional. If set, created cards will be assigned to this Trello member.
TRELLO_MEMBER_ID = os.environ.get("TRELLO_MEMBER_ID")

# Optional. Controls whether 25% confidence tasks are created.
# Recommended: false for production, true while testing.
INCLUDE_LOW_CONFIDENCE_TASKS = (
    os.environ.get("INCLUDE_LOW_CONFIDENCE_TASKS", "false").lower() == "true"
)

# Optional. Marker prefix used for idempotency.
PROCESSED_MARKER_PREFIX = os.environ.get("PROCESSED_MARKER_PREFIX", "processed")

# Optional. Guardrail for very large transcripts.
MAX_TRANSCRIPT_CHARS = int(os.environ.get("MAX_TRANSCRIPT_CHARS", "150000"))


# ---------------------------------------------------------------------
# Vertex AI setup
# ---------------------------------------------------------------------

vertexai.init(project=GCP_PROJECT_ID, location=VERTEX_LOCATION)
model = GenerativeModel(VERTEX_MODEL_NAME)


RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "task_type": {"type": "STRING"},
            "due_date": {"type": "STRING", "nullable": True},
            "priority": {"type": "STRING"},
            "confidence": {"type": "STRING"},
            "source_quote": {"type": "STRING"},
            "reason_assigned_to_nick": {"type": "STRING"},
        },
        "required": [
            "title",
            "description",
            "task_type",
            "due_date",
            "priority",
            "confidence",
            "source_quote",
            "reason_assigned_to_nick",
        ],
    },
}


GENERATION_CONFIG = GenerationConfig(
    temperature=0.1,
    response_mime_type="application/json",
    response_schema=RESPONSE_SCHEMA,
)


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------

def build_prompt(transcript_text: str) -> str:
    return f"""
You are a Product Manager task extraction agent for Nick Henry.

Your job is to analyze a meeting transcript and identify follow-up tasks that Nick should own as the Product Manager.

Focus especially on PM responsibilities such as:
- Clarifying requirements
- Following up with stakeholders
- Driving alignment
- Documenting decisions
- Updating roadmaps, briefs, tickets, or artifacts
- Coordinating design, engineering, operations, clinical, or leadership input
- Preparing leadership updates
- Escalating blockers or unresolved decisions
- Creating or refining user stories, acceptance criteria, workflows, or product requirements
- Investigating risks, dependencies, or open questions

Nick may be referred to as:
- Nick
- Nicholas
- Nicholas Henry
- PM
- Product
- Product Manager
- owner
- DRI
- "you" when the speaker is addressing Nick

Extract only tasks that require action after the meeting.

A valid task must have at least one of the following:
1. A clear owner of Nick/Product/PM
2. A direct ask to Nick
3. A volunteered commitment by Nick
4. A PM-owned follow-up that is strongly implied by the discussion

Do not extract:
- Tasks for other people
- Generic meeting discussion
- Decisions with no follow-up
- Speculation
- Completed actions
- Duplicate or overlapping tasks
- Tasks where ownership is too unclear

For each task, return this exact JSON structure:

[
  {{
    "title": "Verb-led Trello card title under 120 characters",
    "description": "Brief explanation of the task and relevant context",
    "task_type": "follow_up | documentation | stakeholder_alignment | decision_needed | requirements | risk_or_blocker | delivery_coordination | leadership_update | other",
    "due_date": "YYYY-MM-DD or null",
    "priority": "high | medium | low",
    "confidence": "100% | 75% | 50% | 25%",
    "source_quote": "Short quote from the transcript supporting this extraction",
    "reason_assigned_to_nick": "Why this belongs to Nick/Product/PM"
  }}
]

Priority rules:
- Use "high" for blockers, urgent follow-ups, leadership commitments, or tasks needed to unblock others.
- Use "medium" for standard PM follow-ups.
- Use "low" for useful but non-urgent work.

Confidence rules:
- Use "100%" when Nick is directly named or clearly commits.
- Use "75%" when PM ownership is clear or strongly implied but Nick is not directly named.
- Use "50%" when ownership is probable but Nick should verify.
- Use "25%" when ownership is ambiguous or vague.

Important rules:
- Return only valid JSON.
- Do not include markdown.
- Do not include commentary.
- If there are no tasks for Nick, return [].
- Do not invent deadlines.
- Do not invent tasks.
- If a due date is not explicit or strongly implied, use null.
- Keep task titles concise and Trello-ready.
- Make every task title start with a verb.

Transcript:
{transcript_text}
""".strip()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def is_supported_transcript_file(file_name: str) -> bool:
    supported_extensions = (".txt", ".md", ".vtt", ".srt")
    return file_name.lower().endswith(supported_extensions)


def get_processed_marker_name(file_name: str) -> str:
    """
    Creates a safe marker object name for idempotency.

    Example:
    transcripts/foo.txt -> processed/4b1...9e.done
    """
    digest = hashlib.sha256(file_name.encode("utf-8")).hexdigest()
    return f"{PROCESSED_MARKER_PREFIX}/{digest}.done"


def has_been_processed(bucket: storage.Bucket, file_name: str) -> bool:
    marker_name = get_processed_marker_name(file_name)
    return bucket.blob(marker_name).exists()


def mark_as_processed(
    bucket: storage.Bucket,
    file_name: str,
    created_card_count: int,
    extracted_task_count: int,
) -> None:
    marker_name = get_processed_marker_name(file_name)
    marker_blob = bucket.blob(marker_name)

    marker_payload = {
        "source_file": file_name,
        "created_card_count": created_card_count,
        "extracted_task_count": extracted_task_count,
    }

    marker_blob.upload_from_string(
        json.dumps(marker_payload, indent=2),
        content_type="application/json",
    )


def normalize_due_date(value: Optional[str]) -> Optional[str]:
    """
    Trello accepts due dates. This function protects against empty/null strings.
    """
    if not value:
        return None

    value = str(value).strip()

    if not value or value.lower() == "null":
        return None

    return value


def validate_task(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Basic validation and cleanup before sending task to Trello.
    Returns cleaned task or None if invalid.
    """
    if not isinstance(task, dict):
        return None

    title = str(task.get("title", "")).strip()
    description = str(task.get("description", "")).strip()
    confidence = str(task.get("confidence", "")).strip()
    priority = str(task.get("priority", "")).strip().lower()
    task_type = str(task.get("task_type", "")).strip()
    source_quote = str(task.get("source_quote", "")).strip()
    reason = str(task.get("reason_assigned_to_nick", "")).strip()
    due_date = normalize_due_date(task.get("due_date"))

    if not title:
        return None

    if priority not in {"high", "medium", "low"}:
        priority = "medium"

    if confidence not in {"100%", "75%", "50%", "25%"}:
        confidence = "50%"

    if confidence == "25%" and not INCLUDE_LOW_CONFIDENCE_TASKS:
        return None

    # Keep Trello card names reasonably short.
    if len(title) > 120:
        title = title[:117].rstrip() + "..."

    return {
        "title": title,
        "description": description,
        "task_type": task_type or "other",
        "due_date": due_date,
        "priority": priority,
        "confidence": confidence,
        "source_quote": source_quote,
        "reason_assigned_to_nick": reason,
    }


def extract_tasks_with_gemini(transcript_text: str) -> List[Dict[str, Any]]:
    prompt = build_prompt(transcript_text)

    max_attempts = 3
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"Calling Gemini for task extraction. Attempt {attempt}/{max_attempts}.")

            response = model.generate_content(
                prompt,
                generation_config=GENERATION_CONFIG,
            )

            raw_text = response.text or "[]"

            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                print(f"Invalid JSON returned by Gemini: {raw_text}")
                raise ValueError("Gemini returned invalid JSON") from exc

            if not isinstance(parsed, list):
                print(f"Expected JSON array from Gemini, got: {type(parsed)}")
                raise ValueError("Gemini returned non-array JSON")

            cleaned_tasks = []

            for item in parsed:
                cleaned_task = validate_task(item)
                if cleaned_task:
                    cleaned_tasks.append(cleaned_task)

            return cleaned_tasks

        except Exception as exc:
            last_exception = exc
            print(f"Gemini extraction attempt {attempt} failed: {exc}")

            if attempt < max_attempts:
                sleep_seconds = attempt * 5
                print(f"Retrying Gemini extraction in {sleep_seconds} seconds.")
                time.sleep(sleep_seconds)

    raise RuntimeError(
        f"Gemini task extraction failed after {max_attempts} attempts: {last_exception}"
    )


def build_trello_description(task: Dict[str, Any], file_name: str) -> str:
    return f"""
Extracted from: {file_name}

Description:
{task.get("description")}

Task type: {task.get("task_type")}
Priority: {task.get("priority")}
Confidence: {task.get("confidence")}

Source quote:
{task.get("source_quote")}

Why assigned to Nick/Product:
{task.get("reason_assigned_to_nick")}
""".strip()


def create_trello_card(task: Dict[str, Any], file_name: str) -> None:
    """
    Creates a Trello card using Trello's REST API.

    Required environment variables:
    - TRELLO_API_KEY
    - TRELLO_TOKEN
    - TRELLO_LIST_ID
    """
    if not TRELLO_API_KEY:
        raise ValueError("TRELLO_API_KEY is missing")
    if not TRELLO_TOKEN:
        raise ValueError("TRELLO_TOKEN is missing")
    if not TRELLO_LIST_ID:
        raise ValueError("TRELLO_LIST_ID is missing")

    payload = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": task["title"],
        "desc": build_trello_description(task, file_name),
        "pos": "top",
    }

    due_date = task.get("due_date")
    if due_date:
        payload["due"] = due_date

    if TRELLO_MEMBER_ID:
        payload["idMembers"] = TRELLO_MEMBER_ID

    response = requests.post(
        "https://api.trello.com/1/cards",
        data=payload,
        timeout=30,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Trello card creation failed with {response.status_code}: {response.text}"
        )

    card = response.json()
    print(f"Created Trello card: {card.get('name')} ({card.get('shortUrl')})")


# ---------------------------------------------------------------------
# Cloud Function entrypoint
# ---------------------------------------------------------------------

@functions_framework.cloud_event
def process_transcript_for_asana_tasks(cloud_event):
    """
    Cloud Function entrypoint.

    Note: The function name still says "asana" so your existing deploy command
    can keep using --entry-point=process_transcript_for_asana_tasks.
    The implementation now creates Trello cards.
    """
    data = cloud_event.data

    bucket_name = data.get("bucket")
    file_name = data.get("name")

    if not bucket_name or not file_name:
        print(f"Invalid Cloud Storage event payload: {data}")
        return "Invalid event payload", 400

    print(f"Processing transcript: gs://{bucket_name}/{file_name}")

    if file_name.endswith("/"):
        print("Skipping folder placeholder.")
        return "Skipped folder placeholder"

    if not is_supported_transcript_file(file_name):
        print(f"Skipping unsupported file type: {file_name}")
        return "Skipped unsupported file type"

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    # Idempotency check. Avoid duplicate Trello cards if the same file event retries.
    if has_been_processed(bucket, file_name):
        print(f"Skipping already processed file: {file_name}")
        return "Already processed"

    blob = bucket.blob(file_name)

    if not blob.exists():
        print(f"Blob does not exist: gs://{bucket_name}/{file_name}")
        return "File not found", 404

    try:
        transcript_text = blob.download_as_text()
    except Exception as exc:
        print(f"Failed to download transcript text: {exc}")
        raise

    if not transcript_text.strip():
        print("Transcript is empty.")
        mark_as_processed(
            bucket=bucket,
            file_name=file_name,
            created_card_count=0,
            extracted_task_count=0,
        )
        return "Empty transcript"

    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        print(
            f"Transcript length is {len(transcript_text)} chars, "
            f"which exceeds MAX_TRANSCRIPT_CHARS={MAX_TRANSCRIPT_CHARS}. "
            "Processing first MAX_TRANSCRIPT_CHARS characters only."
        )
        transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]

    try:
        extracted_tasks = extract_tasks_with_gemini(transcript_text)
    except Exception as exc:
        print(f"Task extraction failed: {exc}")
        raise

    print(f"Extracted {len(extracted_tasks)} task(s) after validation/filtering.")

    if not extracted_tasks:
        mark_as_processed(
            bucket=bucket,
            file_name=file_name,
            created_card_count=0,
            extracted_task_count=0,
        )
        return "No tasks"

    created_count = 0

    for task in extracted_tasks:
        try:
            create_trello_card(
                task=task,
                file_name=file_name,
            )
            created_count += 1
        except Exception as exc:
            print(f"Unexpected error while creating Trello card '{task['title']}': {exc}")
            raise

    mark_as_processed(
        bucket=bucket,
        file_name=file_name,
        created_card_count=created_count,
        extracted_task_count=len(extracted_tasks),
    )

    print(f"Successfully created {created_count} Trello card(s).")
    return f"Created {created_count} card(s)"