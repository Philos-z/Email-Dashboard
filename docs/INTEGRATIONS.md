# Mail and model integration contracts

Each installation has one mailbox identity and one model configuration. The dashboard password protects all settings and project endpoints. Use same-origin requests with the dashboard session cookie; mutation requests are checked for Origin. `/health` is public and returns no account data.

## Dashboard API

| Method / endpoint | Request / behavior |
| --- | --- |
| `GET /api/mail/config` | Preferences, verification flag and `locked`; no OAuth credentials |
| `PUT /api/mail/config` | `{provider: "outlook" or "gmail", email, timeZone, historySince: "YYYY-MM-DD", mcpServer: "codex_apps", shareWithModel: boolean}`; pauses scheduling, invalidates verification on identity change |
| `POST /api/mail/verify` | `{}`; checks the connector profile against saved email; no message read or model call |
| `GET /api/mail/status` | Enabled/running state, per-folder progress, timezone, recent runs and errors |
| `POST /api/mail/refresh` | `{}`; returns 202 with a job ID or existing job; requires verified account, model config and explicit model-sharing preference |
| `PUT /api/mail/schedule` | `{enabled: boolean}`; hourly schedule with 00:00–06:00 quiet period in the selected timezone |
| `GET /api/ai/config` | `{baseUrl, model, keyConfigured, configured, automaticCalls, mailChat: {enabled, reason}}`; never returns API key |
| `PUT /api/ai/config` | `{baseUrl, apiKey, model}`; blank key preserves saved key only at the same URL |
| `GET /api/ai/models` | Explicit upstream `GET /models` |
| `POST /api/ai/test` | Explicit model call, no project context |
| `POST /api/ai/chat` | `{message, includeContext?: boolean, includeMail?: boolean, history?: [{role, content}]}`; returns `{reply, model, sources, mailAccess}`; no project mutation |

Validation errors use 400, unauthenticated requests 401, conflicting configuration or project revisions 409. Async mail job failures appear in `status.runs` and do not advance cursors. Credentials and private response snippets are excluded from upstream error messages.

The model endpoint must implement OpenAI-compatible `/models` (optional if entering the model name directly) and `/chat/completions` with `choices[].message.content`. Mail chat additionally requires function tools: `tools`, `tool_choice`, `choices[].message.tool_calls` and `role: "tool"` messages. Requests use a Bearer API key, redirects are disabled, and system HTTP proxy environment variables are ignored. Native Anthropic / Gemini request formats require an OpenAI-compatible gateway.

### Read-only Outlook assistant

`assistant_chat.py` exposes only `search_outlook_mail`, `list_outlook_mail` and `read_outlook_mail`. The backend maps these to the configured MCP server's `microsoft_outlook_email.search_messages`, `list_messages` and `fetch_message`. It verifies the configured account through `mail_provider.connect` before reading any message, honors `shareWithModel`, and rejects changed settings before sending retrieved content to the model. Gmail live chat is not enabled in this version; Gmail sync and project-summary chat remain available.

`includeContext` defaults to false in the API; the UI selects it and sends project summaries plus `contextSummary`, excluding notes and full evidence. `includeMail` defaults to the installation's `mailChat.enabled`; an explicit true requires a verified Outlook account and model-sharing permission. The UI disables the mail option and shows the reason otherwise. Ordinary chat does not open MCP unless the model requests a mail tool.

Search/list results include up to 6000 characters of body per message. Full reads, intended only for explicit full-body requests, require a source discovered in the same request and are limited to 20000 characters. Pagination uses only returned cursors. Each request is limited to six tool calls, four query rounds, 60 distinct messages and 240 seconds. Sources use stable per-request `M1` labels and validated HTTPS Outlook links; the UI displays query counts, source dates in the installation timezone and pagination warnings. No attachments, mailbox writes, project changes or sync-cursor changes occur. User-triggered queries work during scheduled quiet hours. Chat history remains in page memory.

## Adapter boundary

`mail_provider.connect(config)` yields the `Mailbox` protocol in `mail_provider.py`. It verifies `profile()` before any folder or message call. Add or customize provider logic here rather than in the sync or frontend layers.

- `own_address`: connector account email, matched case-insensitively against user configuration.
- `ordered`: whether pages promise oldest-first order.
- `list_watched_folders()` returns `[{id, path, kind, direction}]`; kind is inbox/archive/sentitems, direction received/sent.
- `list_messages(since, through, skip=0, top=50, folder_id=..., direction=...)` returns `{value: [canonical raw message], has_more: bool, next_from_index: int|string}`. `since`/`through` are fixed UTC bounds. Cursor is 0 initially, Outlook integer offset or Gmail opaque token afterwards.
- Canonical messages carry `id`, `subject`, `receivedDateTime`, optional `sentDateTime`, `from.emailAddress.address`, and `body.contentType/content`.

Folder checkpoints and opaque cursor JSON persist in SQLite. A window advances only after all pages and model updates commit successfully. Gmail pages need not be chronological; final display sorts by mail time. Repeated/invalid page cursors and incomplete reads fail without committing progress. MIME bodies are converted to text; attachment parts are excluded from model input. Gmail's batch read returns MIME trees which may include attachment metadata or embedded attachment data; the dashboard does not call attachment-fetch tools or use those parts.

## MCP transport

The bundled transport launches the user's local `codex app-server --stdio`, creates an ephemeral thread context and calls `mcpServer/tool/call`. No `turn/start` or Codex model request is issued. Interactive authorization/elicitation requests fail closed; the user must resolve them in their own client. MCP server alias can be configured; executable path can only be set by the deployment environment, not by web requests.

Required tool names and arguments:

| Provider | Tool | Arguments |
| --- | --- | --- |
| Outlook | `microsoft_outlook_email.get_profile` | `{}`; structuredContent.email |
| Outlook | `microsoft_outlook_email.list_mail_folders` | `{include_hidden_folders:false, top:500}`; structuredContent.value with folder metadata |
| Outlook | `microsoft_outlook_email.list_messages` | `{folder_id, filter, order_by, skip, top}`; structuredContent.value, has_more, next_from_index |
| Gmail | `gmail.get_profile` | `{}`; structuredContent.emailAddress/email/email_address |
| Gmail | `gmail.search_email_ids` | `{query, max_results, next_page_token}`; structuredContent.ids/message_ids/messages, next_page_token/nextPageToken |
| Gmail | `gmail.batch_read_email` | `{message_ids:[...]}`; structuredContent.messages/emails or list of MIME message objects |

Gmail message objects use the Gmail `id`, `internalDate` (milliseconds), `payload.headers` and nested `payload.parts/body.data` base64url MIME structure. Response field differences belong in `gmail_mcp.py`. Unsupported schemas produce a safe failure rather than silently advancing the checkpoint. Connector implementations exposing different tool names require an adapter shim; a server alias alone does not translate arbitrary third-party tools.

Gmail scopes: `in:inbox`; archive `-in:inbox -in:sent`; `in:sent`. All exclude spam/trash/drafts. Queries use epoch seconds with a one-second boundary overlap and precise local filtering. Gmail `internalDate` is the query/checkpoint clock even for Sent; aliases without a SENT label may require a custom adapter.

Sources checked during implementation: [OpenAI app-server protocol](https://developers.openai.com/codex/app-server/), [Gmail search filtering and epoch dates](https://developers.google.com/workspace/gmail/api/guides/filtering), [Gmail message/MIME resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages). Current connector input schemas were inspected without reading mailbox content. Live Gmail end-to-end compatibility remains for the owner's first manual refresh; automated tests use synthetic responses.
