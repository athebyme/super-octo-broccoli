Implementation contract; not yet deployed.

Root integration: `GET /review` (HTML) and `/api/review` (JSON) take `account_id`, comma-separated `draft_ids` (1..200 exact distinct IDs), optional `parent_prepare_job_uid`, and `page`. The entire selection must be owned; each page contains at most 20 freshly validated rows with exact current versions, errors/warnings, create/update action, type and photo. GET neither prepares nor mutates drafts and returns private/no-store. Classic HTML includes a server-generated request key and current-page version map; it submits only checked IDs on that page. Vue can preserve actually reviewed selectable IDs/versions across pages. JSON publish requires the exact selected version map. Source and publish POST reject unknown/duplicate JSON fields and query/body scope mixing; JSON body cap64KiB. `draft_review_required` is HTTP409.

# Ozon upload v3 service interfaces — implementation stub

`OzonBulkUploadService.accept_source_prepare(*, seller_id:int, account_id:int, imported_product_ids:list[int], request_key:str, created_by_user_id:int|None=None) -> UploadRunAcceptance`
- Local owner/account/product-set precheck only; `MARKETPLACE_OZON_ENABLED` and active owned account required; publication flag, credential state and provider not required.
- One commit inserts BackgroundJob+Run(mode=source_prepare)+ordered Items(phase=pending). Duplicate same key/fingerprint returns same job; changed fingerprint 409. No provider/media I/O, no drafts or operations created in POST.

`OzonBulkUploadService.accept_reviewed_publish(*, seller_id:int, account_id:int, draft_ids:list[int], expected_versions:dict[str,int], request_key:str, parent_prepare_job_uid:str|None=None, created_by_user_id:int|None=None) -> UploadRunAcceptance`
- Flags/account/credentials and exact seller/account/ready/version ownership checked at acceptance; snapshot ordered item IDs/version and optional exact parent provenance. No draft mutation or provider/media I/O in POST. One commit inserts job+run(mode=reviewed_drafts)+Items(phase=reviewed). Duplicate behavior as above.

`@dataclass(frozen=True) UploadRunAcceptance(job:BackgroundJob, replayed:bool)` is returned by both accept methods. The `replayed` flag is decided from the unique insert/IntegrityError recovery path, never from a race-prone prelookup alone. Routes serialize `acceptance.job` and `acceptance.replayed`.

`OzonBulkUploadService.find_by_request_key(*, seller_id:int, account_id:int, request_key:str) -> BackgroundJob`
- Read-only seller+account scoped lookup; 404 if absent. Route uses `X-Upload-Request-Key` header; no POST replay or provider access.

`OzonBulkUploadService.run_due_preparation(*, now:datetime|None=None, run_limit:int=20, item_limit:int=40, seconds_budget:int=45) -> dict`
- Existing singleton scheduler calls once/tick; no new thread. Returns exactly `{selected:int, processed_items:int, prepared:int, enqueued:int, waiting:int, needs_input:int, busy:int, failed:int}`. Claims due run via CAS lease; selected counts successfully claimed, busy skipped without slot. Round-robin across due runs, max20 items per run/tick. `source_prepare` ends at prepared/needs_input; `reviewed_drafts` uses atomic enqueue helper. No provider writes or reads in worker.

`OzonBulkUploadService.reconcile_active_runs(*, limit:int=20, now:datetime|None=None) -> dict`
- Returns `{selected:int, reconciled:int, failed:int}` and mirrors operation FK status only. No loose draft lookup for v3, no provider calls. Legacy adoption is bounded to `limit` active jobs/tick; ambiguous legacy item becomes manual reconciliation and cannot queue a new write.

`OzonBulkUploadService.get_run(*, seller_id:int, job_uid:str, reconcile:bool=True) -> BackgroundJob`; `.public_document(job, detail:bool=False) -> dict`
- V3 output `{job_uid,status,mode,account_id,account_label,summary,created_at,updated_at,items?}`. Source summary has `prepared/needs_input/waiting_reference`, no `created/updated` success; reviewed summary carries linked operation results. Each item includes `imported_product_id,title,phase,status,action?,draft_id?,reviewed_version?,operation_id?,offer_id?,code?,message?,updated_at` with typed public data only. Existing v1/v2 public shape remains readable.

`OzonBulkUploadService.retry_run(*, seller_id:int, job_uid:str, request_key:str, created_by_user_id:int|None=None, expected_versions:dict[str,int]|None=None) -> UploadRunAcceptance`
- Source run: explicit new local-only preparation run for selected safe terminal items. Reviewed run: 409 `draft_review_required` unless a fresh exact version map and explicit publish confirmation are provided via `accept_reviewed_publish`; no implicit source-mode conversion or uncertain replay.

`OzonUploadQueueService.claim_due_run(*, now:datetime, exclude_run_ids:set[int]) -> (run_id:int, lease_token:str)|None`; `.lease_current(*,run_id:int,lease_token:str,now:datetime) -> bool`; `.advance_item(*,run_id:int,item_id:int,lease_token:str,now:datetime) -> ItemAdvanceResult`; `.release_run(*,run_id:int,lease_token:str,now:datetime,next_due_at:datetime|None) -> None`.
- Each transition after a local service commit CAS-checks BOTH token and unexpired lease. Stale worker cannot commit a later state. Local exceptions re-read exact item/op status before limited retry. `ItemAdvanceResult` has `{processed:bool, phase:str, outcome:str, operation_id:int|None, next_due_at:datetime|None}`. Due ordering `(next_due_at,last_attempt_at,id)`; no-change waiting run gets future due and updated last attempt.

`MarketplacePublicationService.enqueue_reviewed_upload_item(*, seller_id:int, account_id:int, draft_id:int, expected_version:int, run_item_id:int, lease_token:str, created_by_user_id:int|None, now:datetime) -> MarketplaceOperation`
- Private helper; validates run/item/seller/account/reviewed identity, current token+expiry, exact draft version, schema/payload/quarantine; starts short SQLite write transaction and verifies token+expiry again INSIDE the transaction. Creates operation+snapshot+updates item.operation_id/phase and BackgroundJob summary in ONE commit. Existing active operation on that draft returns typed `already_in_progress` (no cross-run item link); only an already-linked exact operation for the same item is idempotent. Never performs provider I/O. `_create_operation` gets an internal commit=False path while public existing calls keep current behavior.

Route contract root owns: POST `/marketplaces/ozon/uploads/` strict `{account_id,imported_product_ids,confirm_prepare:true,request_key}`; POST `/from-drafts` strict `{account_id,draft_ids,expected_versions,confirm_write:true,request_key,parent_prepare_job_uid?}`. JSON first receipt 202 + Location + `{success:true,run:public_document,replayed:false}`; dedup same UID `replayed:true`; classic POST 303 to same job. Old source-sync `{confirm_write:true}` fails typed `review_required`, never silently reinterpreted. `GET /api/by-request` with account_id and key header returns scoped run or 404. All routes remain CSRF/tenant-scoped.


Root scheduling delta (2026-09-27): the existing singleton APScheduler registers a separate `prepare_ozon_uploads` job every10s, max_instances=1/coalesce, invoking run_limit20/item_limit40/seconds_budget8. No custom thread/executor is introduced. The minute provider-operation poll remains independent. Service default45s is a maximum callable ceiling, not the runtime scheduler budget. Measured 200 real local missing-data preparations:10 ticks, max0.794s/tick,6.577s total,181248KiB peakRSS,0 provider calls/operations; ready-card preparation and actual publication are separate gates.
