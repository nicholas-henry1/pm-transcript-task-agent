# pm-transcript-task-agent

An event-driven AI agent that captures meeting transcripts, moves them into Google Cloud Storage, uses Vertex AI with Gemini 2.5 Flash to extract Product Manager action items, and routes them into Trello as reviewable cards.

The original workflow routed tasks into Asana. The current workflow routes extracted tasks into Trello.

---

## End-to-End PM Transcript-to-Trello Card Agent

This project implements an automated transcript-to-task pipeline for Product Manager follow-ups.

The system listens for new meeting transcript artifacts, ships them to Google Cloud Storage, triggers a serverless Python function, extracts PM-owned follow-up tasks with Gemini, and creates Trello cards in a review lane.

Current production target:

- Trello board: `AI Task Intake`
- Trello list: `Needs Review`
- Trello list ID: `6a1ed585c94550725be836b3`

---

## Platform Stack & Tools

### Google Workspace / Apps Script

A Google Apps Script listens for new transcript uploads or transcript file availability in Google Workspace.

The Apps Script acts as the upstream handoff layer. When a transcript is available, it uploads or forwards the transcript file into the configured Google Cloud Storage bucket.

This gives the workflow an automated front door:

    Transcript generated/uploaded in Google Workspace
    → Apps Script detects or handles the file
    → Apps Script moves/uploads transcript to Google Cloud Storage
    → Cloud Storage event triggers the PM agent

### Google Cloud Platform

- Cloud Storage: Stores transcript files and emits object-created events.
- Cloud Functions Gen 2 / Cloud Run: Runs the Python processing function.
- Python 3.10: Current runtime.
- Processed marker files: Prevent duplicate Trello card creation.

### Vertex AI

- Gemini 2.5 Flash: Extracts PM-owned action items from transcript text.
- Structured JSON response schema: Enforces consistent task output.
- Retry handling: Retries transient Gemini / Vertex AI failures such as temporary `503` errors.

### Trello

- Trello REST API: Creates cards directly in Trello.
- Target board: `AI Task Intake`
- Target list: `Needs Review`
- Purpose: Keeps AI-generated tasks in a reviewable intake lane before they become committed work.

---

## System Architecture

The workflow is an event-driven loop across Google Workspace, Google Cloud, Vertex AI, and Trello.

### 1. Transcript Capture

A meeting transcript or Gemini meeting note artifact is created or uploaded in Google Workspace.

The upstream Apps Script listens for the transcript event or handles transcript movement from the source location.

### 2. Apps Script Handoff

The Apps Script uploads the transcript file into the Google Cloud Storage bucket:

    gs://nick-transcripts-1778251680/

This step is the bridge between Google Workspace and the cloud-native processing pipeline.

### 3. Cloud Storage Ingestion

The transcript lands in Cloud Storage.

Currently supported transcript file types:

- `.txt`
- `.md`
- `.vtt`
- `.srt`

Unsupported files are skipped.

Example skip behavior:

    Processing transcript: gs://nick-transcripts-1778251680/example.docx
    Skipping unsupported file type: example.docx

### 4. Event Trigger

The bucket fires a Cloud Storage event that triggers the Gen 2 Cloud Function:

    pm-agent-v3

The current deploy entry point remains:

    process_transcript_for_asana_tasks

Note: The entry point still contains `asana` in the name for deployment continuity. The implementation now creates Trello cards.

### 5. Idempotency Guard

The function generates a SHA-256 hash of the source filename and checks whether a processed marker already exists.

Marker location:

    processed/<sha256-of-filename>.done

If the marker already exists, the function skips the file to avoid duplicate Trello cards.

### 6. Transcript Download

The Cloud Function downloads the transcript text from the bucket.

If the transcript is empty, the function writes a processed marker and exits without creating cards.

### 7. AI Processing with Vertex AI

Gemini 2.5 Flash analyzes the transcript and returns structured JSON containing PM-owned follow-up tasks.

The model is configured with:

- Low temperature
- Strict JSON response schema
- PM-specific extraction instructions
- Confidence scoring
- Priority scoring

### 8. Gemini Retry Handling

The Gemini call is wrapped in retry logic.

Current retry behavior:

    Attempt 1
    → if failed, wait 5 seconds

    Attempt 2
    → if failed, wait 10 seconds

    Attempt 3
    → if failed, raise error

This was added after observing transient Vertex AI errors such as:

    503 Stream removed (recvmsg:Connection reset by peer)

Expected retry log:

    Calling Gemini for task extraction. Attempt 1/3.

### 9. Trello Card Creation

Each validated extracted task is converted into a Trello card.

Cards are created in:

    AI Task Intake → Needs Review

The Trello card description includes:

- Source transcript filename
- Task description
- Task type
- Priority
- Confidence
- Source quote
- Reason assigned to Nick/Product

### 10. Idempotency Finalization

After successful Trello card creation, the function writes a `.done` marker to the `processed/` folder.

This safely concludes the execution loop.

### 11. Processed Marker Skip

Because the marker is written to the same bucket, it triggers the function again.

This is expected.

The function safely skips `.done` files because they are unsupported file types.

Example log:

    Processing transcript: gs://nick-transcripts-1778251680/processed/<hash>.done
    Skipping unsupported file type: processed/<hash>.done

---

## Current Workflow Summary

    Google Workspace transcript
    → Apps Script listener/handoff
    → Google Cloud Storage bucket
    → Cloud Function Gen 2
    → Vertex AI Gemini 2.5 Flash
    → Trello REST API
    → Trello card in AI Task Intake / Needs Review
    → processed marker written to GCS

---

## Vertex AI Core Prompt

The extraction logic uses Gemini 2.5 Flash with a structured JSON schema.

The model acts as a Product Manager task extraction agent for Nick Henry.

It focuses on follow-up work such as:

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
- “you” when the speaker is addressing Nick

A valid extracted task must have at least one of the following:

1. A clear owner of Nick/Product/PM
2. A direct ask to Nick
3. A volunteered commitment by Nick
4. A PM-owned follow-up that is strongly implied by the discussion

The agent does not extract:

- Tasks for other people
- Generic meeting discussion
- Decisions with no follow-up
- Speculation
- Completed actions
- Duplicate or overlapping tasks
- Tasks where ownership is too unclear

---

## Extracted Task JSON Shape

Gemini returns a JSON array of task objects.

Example:

    [
      {
        "title": "Verb-led Trello card title under 120 characters",
        "description": "Brief explanation of the task and relevant context",
        "task_type": "follow_up | documentation | stakeholder_alignment | decision_needed | requirements | risk_or_blocker | delivery_coordination | leadership_update | other",
        "due_date": "YYYY-MM-DD or null",
        "priority": "high | medium | low",
        "confidence": "100% | 75% | 50% | 25%",
        "source_quote": "Short quote from the transcript supporting this extraction",
        "reason_assigned_to_nick": "Why this belongs to Nick/Product/PM"
      }
    ]

---

## Priority Rules

- `high`: Blockers, urgent follow-ups, leadership commitments, or work needed to unblock others.
- `medium`: Standard PM follow-ups.
- `low`: Useful but non-urgent work.

---

## Confidence Rules

- `100%`: Nick is directly named or clearly commits.
- `75%`: PM ownership is clear or strongly implied but Nick is not directly named.
- `50%`: Ownership is probable but Nick should verify.
- `25%`: Ownership is ambiguous or vague.

Low-confidence task behavior is controlled by:

    INCLUDE_LOW_CONFIDENCE_TASKS=true

For production, this can be set to:

    INCLUDE_LOW_CONFIDENCE_TASKS=false

---

## Configuration & Environment Variables

The function relies on the following environment variables.

| Variable Name | Description | Example / Format |
| :--- | :--- | :--- |
| `GCP_PROJECT_ID` | GCP project ID | `ascn-win-visit-3287-sbx` |
| `VERTEX_LOCATION` | Vertex AI region | `us-central1` |
| `VERTEX_MODEL_NAME` | Vertex AI model | `gemini-2.5-flash` |
| `TRELLO_API_KEY` | Trello API key | Secure deploy-time value |
| `TRELLO_TOKEN` | Trello user token | Secure deploy-time value |
| `TRELLO_LIST_ID` | Target Trello list ID | `6a1ed585c94550725be836b3` |
| `TRELLO_MEMBER_ID` | Optional Trello member ID for assignment | Optional |
| `INCLUDE_LOW_CONFIDENCE_TASKS` | Whether to include 25% confidence tasks | `true` or `false` |
| `PROCESSED_MARKER_PREFIX` | Prefix for processed marker files | `processed` |
| `MAX_TRANSCRIPT_CHARS` | Max transcript size processed | `150000` |

Current Trello destination:

| Trello Object | Value |
| :--- | :--- |
| Board | `AI Task Intake` |
| List | `Needs Review` |
| List ID | `6a1ed585c94550725be836b3` |

---

## Deploy Command

Deploy the function from the current branch with:

    gcloud functions deploy pm-agent-v3 \
      --gen2 \
      --runtime=python310 \
      --region=us-central1 \
      --entry-point=process_transcript_for_asana_tasks \
      --trigger-bucket=nick-transcripts-1778251680 \
      --service-account="pm-agent-identity@ascn-win-visit-3287-sbx.iam.gserviceaccount.com" \
      --build-service-account="projects/ascn-win-visit-3287-sbx/serviceAccounts/pm-agent-identity@ascn-win-visit-3287-sbx.iam.gserviceaccount.com" \
      --timeout=120s \
      --set-env-vars GCP_PROJECT_ID="ascn-win-visit-3287-sbx",VERTEX_LOCATION="us-central1",VERTEX_MODEL_NAME="gemini-2.5-flash",TRELLO_API_KEY='YOUR_TRELLO_API_KEY',TRELLO_TOKEN='YOUR_TRELLO_TOKEN',TRELLO_LIST_ID='6a1ed585c94550725be836b3',INCLUDE_LOW_CONFIDENCE_TASKS=true

Note: The entry point is still named `process_transcript_for_asana_tasks` for deployment continuity. The implementation now creates Trello cards.

---

## End-to-End Test

Create a test transcript:

    cat > trello_final_path_test_001.txt <<'EOF'
    Meeting notes:

    Nick will verify that the Trello-only Cloud Function deployment creates cards in the Needs Review list.

    Nick will confirm that no new Asana tasks are created by the transcript workflow.
    EOF

Upload it to the bucket:

    gcloud storage cp trello_final_path_test_001.txt gs://nick-transcripts-1778251680/

Read logs:

    gcloud functions logs read pm-agent-v3 \
      --region=us-central1 \
      --limit=60

Useful filtered logs:

    gcloud functions logs read pm-agent-v3 \
      --region=us-central1 \
      --limit=100 | grep -E "Processing transcript|Calling Gemini|Extracted|Created Trello card|Successfully created|Unexpected error|Task extraction failed"

Expected success pattern:

    Processing transcript: gs://nick-transcripts-1778251680/example.txt
    Calling Gemini for task extraction. Attempt 1/3.
    Extracted 1 task(s) after validation/filtering.
    Created Trello card: Example task title (https://trello.com/c/...)
    Successfully created 1 Trello card(s).

---

## How to Force Process or Reprocess a Transcript

Cloud Storage triggers are event-based. To force a new run, use a new filename or overwrite an existing file.

### Recommended: upload with a new filename

    cp original_transcript.txt original_transcript_retry_001.txt

    gcloud storage cp original_transcript_retry_001.txt gs://nick-transcripts-1778251680/

### Alternative: overwrite the object

    gcloud storage cp gs://nick-transcripts-1778251680/your_target_transcript.txt gs://nick-transcripts-1778251680/your_target_transcript.txt

If a `.done` marker already exists for that filename, the function will skip the file as already processed. In that case, use a fresh filename.

---

## Apps Script Handoff Layer

The upstream Apps Script is responsible for moving new transcript files from the Google Workspace side into Google Cloud Storage.

Conceptually, the Apps Script does the following:

1. Detects or receives a transcript file.
2. Reads the transcript content or file blob.
3. Authenticates to the Google Cloud Storage upload target.
4. Uploads the transcript into:

       gs://nick-transcripts-1778251680/

5. The Cloud Storage object creation event triggers the serverless PM agent.

The Apps Script layer is intentionally separate from the Cloud Function. Its job is not to run AI extraction. Its job is only to move transcript artifacts into the bucket reliably.

The Cloud Function owns:

- File validation
- Idempotency checks
- Gemini extraction
- Trello card creation
- Processed marker writing

---

## Processed Marker Behavior

For each successfully processed file, the function writes a marker file to:

    processed/<sha256-of-filename>.done

Example log:

    Processing transcript: gs://nick-transcripts-1778251680/processed/<hash>.done
    Skipping unsupported file type: processed/<hash>.done

This is expected behavior. The `.done` marker triggers the function because it is written to the same bucket, but the function skips it because `.done` is not a supported transcript file type.

---

## Reliability / Resiliency

The function includes retry handling around Gemini extraction.

Current behavior:

    Attempt 1 → wait 5 seconds if failed
    Attempt 2 → wait 10 seconds if failed
    Attempt 3 → fail if still unsuccessful

This was added after observing transient Vertex AI errors such as:

    503 Stream removed (recvmsg:Connection reset by peer)

---

## Dependencies

The function uses:

    functions-framework
    google-cloud-storage
    google-cloud-aiplatform
    requests

These are defined in:

    requirements.txt

---

## Latest Verified Test Evidence

The current Trello-backed workflow has been tested successfully.

Cloud Function logs confirmed:

    Processing transcript: gs://nick-transcripts-1778251680/TRANSCRIPT OF PRODUCT MEETING 8_2.txt
    Calling Gemini for task extraction. Attempt 1/3.
    Extracted 1 task(s) after validation/filtering.
    Created Trello card: Coordinate API Gateway security audit and O2AUTH enablement (https://trello.com/c/kcFbNoXX)
    Successfully created 1 Trello card(s).

This confirms the working path:

    Google Workspace transcript
    → Apps Script upload to GCS
    → Cloud Storage trigger
    → Cloud Function
    → Gemini extraction
    → Trello card creation

---

## Git / Branching Notes

Recent feature work was completed on:

    feature_2

Primary changes:

- Migrated output from Asana task creation to Trello card creation.
- Added Trello API configuration.
- Added Gemini retry handling.
- Renamed dependency file to `requirements.txt`.
- Preserved existing Cloud Function entry point for deployment stability.

---

## Future Improvements

Potential next enhancements:

- Move Trello token and API key to Google Secret Manager.
- Rename the Cloud Function entry point from `process_transcript_for_asana_tasks` to a neutral name.
- Rename deployed function from `pm-agent-v3` to something like `pm-transcript-task-agent`.
- Add `.docx` support for Gemini-generated meeting notes.
- Add Trello labels for priority and confidence.
- Add duplicate detection based on task title and source transcript.
- Add dry-run mode for review before card creation.
- Add Slack or email summary after each processing run.
- Add routing rules for different task types.
- Add structured logging for easier monitoring.
- Add a separate bucket or prefix for processed markers to avoid marker-trigger noise.
