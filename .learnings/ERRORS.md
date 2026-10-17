# Development errors

- 2026-10-16: Official documentation search tool returned HTTP 429. No documentation claims inferred from the error. Validate SDK calls against installed dependencies and the isolated integration environment.
- 2026-10-16: Local uv default mirror returned 403; use explicit PyPI index on commands. Server default Docker build network could not resolve PyPI; building this image with host networking succeeded without changing Docker daemon or other services.
- 2026-10-16: Closing the React Monaco DiffEditor wrapper disposed models before detaching the diff widget. Replaced with an explicit diff lifecycle: detach models, dispose widget, then dispose models. Browser regression covers repeated opening and closing.
- 2026-10-16: Recreating controlled React Flow nodes on SSE updates discarded measured dimensions, hiding nodes after refresh. Preserve dimensions events separately from topology so observation refreshes do not reset visibility or dirty the draft.
