keywords: retime, re-time, scc, caption files, captions cut, cuts, zero-based, zero based, speaker label, sound descriptor, speech to text, whisper captions
title: Retime captions to a cut video (Speech to Text)

Speech to Text > "Retime captions to a cut video" fits caption files onto a video that has had parts cut out.
Use it when the captions come as several files (one per part, each starting at zero, with no cuts) and the video is all the parts in one file with some content removed.
1. Pick the combined video (Browse library) and add all the caption files (.scc, .srt or .vtt). Parts are taken in file-name order, so name them part1, part2, part3.
2. Optionally tick Remove speaker labels and/or Remove sound descriptors. Unticked, both stay in the captions as written. Ticked, they come out of the .srt; the .scc always keeps the original captions and only has its timecodes moved.
3. Choose what happens to a caption it cannot place with confidence: keep it placed between its neighbours (flagged) or drop it. Choose the spoken language if you know it (Tagalog or English; Auto-detect works for a mix), then press Retime captions. A full episode takes a few minutes.
How it works: the video is transcribed with Whisper. Each caption is matched to the transcript by how much its letters resemble the speech, so Whisper's Tagalog spelling mistakes do not matter. Strong matches become anchors; the spacing inside each caption file places the rest. Where the offset between anchors gets smaller, a cut is found, and the captions inside it are dropped. Speaker labels and sound descriptors are not used for matching.
The result lists every caption as matched, placed (from its neighbours), check this (low confidence, review these) or in a cut (dropped), with the cuts found. Download the .srt, and the .scc when every caption file is an .scc.

## Fewer dropped captions

- A caption sitting between two matched captions that agree on the offset (no cut between them) is always kept, even if Whisper did not hear it (music, sound tags, mumbled lines). Only captions before the first match, after the last, or inside a detected cut can be flagged or dropped.
- Matching treats Filipino spelling variants as the same: o/u, e/i, and apostrophe contractions (s'ya = sya).
- The summary counts "unsure (dropped)" separately from "in cuts".
- Still dropped/flagged when it matters: captions in a detected cut, and captions Whisper cannot support before the first or after the last match. If many are still flagged, set the language to Tagalog (helps Whisper) and keep "Keep it next to its neighbours" to review them.
