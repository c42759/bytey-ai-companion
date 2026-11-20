---
name: summarize-clipboard
description: Summarize, clean up, or extract key points from pasted or clipboard text.
version: 1.0.0
slash_command: summarize
requires_confirmation: false
allowed_tools: []
---

# Summarize Clipboard

When the user provides text or asks to summarize clipboard content:

1. **Identify the format**: Detect whether the text is an article, meeting notes, email thread, log snippet, or raw notes.
2. **Produce a concise summary**:
   - Start with a 1-2 sentence TL;DR.
   - Follow with 3-5 crisp bullet points capturing key facts, decisions, or action items.
3. **Preserve accuracy**: Do not invent details, dates, or names not present in the source text.
4. **Offer a follow-up**: If action items or unclear phrasing exist, briefly offer to rewrite or extract tasks.
