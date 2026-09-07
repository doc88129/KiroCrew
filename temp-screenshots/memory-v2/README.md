# Memory V2 demo

Captured from source revision `d7d05392f3e6be5bc4de0b6f5abd75d669db0633` on an
isolated Linux gateway. Mira and Theo are synthetic demo members. The screenshots
and video show the real application with test data.

The 54.36-second WebM records member identity, private memory, a correction preview,
the saved correction after reload, bounded recall, backup creation, a separate
empty member store and member conversation navigation. It does not show a live
model response or a Crew Mode delegation run.

Recall in this isolated demo uses keyword fallback with model downloads disabled.
The member header and drawer reflect the integrated React Query member interface.

Desktop captures are 1440 by 900 pixels. Mobile captures are 390 by 844 pixels.
Verification confirmed that the correction persists after reload, Theo's store
stays empty, raw retrieval context stays collapsed until requested, and cancelling
the mobile correction preview leaves the saved memory unchanged.

The video uses VP8 in a WebM container. The numbered PNG files cover eight desktop
views and four mobile views. These review artifacts are outside the packaged app.
