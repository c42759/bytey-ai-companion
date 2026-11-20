---
name: code-review
description: Review code snippets or diffs for bugs, edge cases, security risks, and clarity.
version: 1.0.0
slash_command: review
requires_confirmation: false
allowed_tools: []
---

# Code Review

When reviewing code or a diff provided by the user:

1. **Correctness & Edge Cases**: Check for off-by-one errors, null/None handling, race conditions, and unhandled exceptions.
2. **Security**: Flag injection risks, path traversal, hardcoded secrets, or unsafe deserialization.
3. **Clarity & Maintainability**: Note duplicated logic, overly deep nesting, or misleading naming.
4. **Prioritized Feedback**:
   - Group findings by severity: **Critical**, **Warning**, **Suggestion**.
   - Reference exact symbol names or lines and provide concise, actionable fixes.
