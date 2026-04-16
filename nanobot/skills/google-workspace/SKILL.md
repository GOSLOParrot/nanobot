---
name: google-workspace
description: "Use when you need to access Gmail, Google Calendar, or Google Drive via MCP. Triggered by task 'message_check', 'calendar_fetch', or explicit user requests for Google."
---

# Google Workspace Skill

This skill guides you on how to use the Google Workspace MCP server (`@aaronsb/google-workspace-mcp`).

## Authentication
If you receive an error about missing credentials or authentication:
1. Call the `manage_accounts` tool with `{"operation": "authenticate"}`.
2. Instruct the user to complete the OAuth flow in the browser that opens on their machine.

## Gmail
When tasked with `message_check` or reading Gmail:
- Use the `manage_email` tool.
- To search for unread, important emails, use `{"operation": "search", "query": "is:unread (is:important OR is:starred)"}`.
- Extract sender, subject, snippet, timestamp, and importance for each result. Return them as a structured JSON array if requested by the task.

## Calendar
When tasked with `calendar_fetch` or reading Google Calendar:
- Use the `manage_calendar` tool.
- To list today's events, use `{"operation": "list_events", "timeMin": "<start_of_day_iso>", "timeMax": "<end_of_day_iso>"}`.
- Extract event ID, title, start_time, end_time, location, and description.

## Important Note on Task Output
If you are responding to a background task (like `calendar_fetch` or `message_check`), you **MUST** format your final output strictly as the requested JSON array structure, without conversational filler.
