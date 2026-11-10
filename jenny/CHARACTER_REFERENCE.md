# Jenny Carter — Character Reference (Source of Truth)

> **Purpose:** the single place that locks *how Jenny looks* so every image/video we
> generate stays consistent. Read this before writing any generation prompt.
> Full personality/story lives in `../persona.md`; this file is the **visual + platform** lock.

_Last updated: 2026-10-15_

---

## 1. Identity lock (keep constant in EVERY render)

- **Who:** Jenny Carter — fictional AI fitness/tennis influencer. **Original character — not a real person.**
- **Apparent age:** reads **early twenties, youthful and fresh-faced** — *but unmistakably a grown adult woman.*
  (Persona canon age is 25; we skew her look slightly younger per creator direction. See §4.)
- **Face:** oval face, defined cheekbones, clean straight nose, full natural lips with a warm
  rosy-nude tone, soft defined jawline. Warm, friendly, approachable expression.
- **Eyes:** bright **blue-green**, almond-shaped, naturally muted catchlights (no glassy AI glare).
- **Eyebrows:** soft, natural medium-blonde, lightly arched.
- **Hair:** long **honey/golden blonde balayage** with lighter face-framing money-pieces, natural
  soft waves, center-ish part, healthy lived-in texture (not glassy-perfect).
- **Skin:** light **golden tan**, real skin texture — visible pores, a few faint freckles/small
  moles on torso, natural (slightly uneven) makeup. No airbrushed/plastic look.
- **Body:** athletic, lean, **toned fitness build** — visible abs, balanced natural proportions.
- **Signature continuity details (do not drop):**
  - **Thin fabric scrunchie on her LEFT wrist** in most shots (story payoff from persona).
  - **Small gold heart pendant necklace** (seen across her existing content).
  - Small delicate stud earrings.

## 2. Hard rules

- **Consistency is the product.** Same face/hair/body across every render. Change only
  scene, outfit, pose, lighting, camera — never her identity.
- **Original character only.** Never match a real celebrity or real person's likeness.
- **Adults only (18+).** Jenny is an adult; never render her to look like a minor. "Younger"
  means *fresh-faced early-twenties*, never childlike. Keep all content within
  Instagram / Fanvue platform rules.
- **Never spend Higgsfield credits / run a generation without the creator's explicit go-ahead.**

## 3. Higgsfield platform anchors (reuse these — don't recreate)

| Thing | Name | ID |
|---|---|---|
| Workspace | (private, Pro) | `147b677f-06ca-4637-b8bd-725d6c173528` |
| Project folder | **Jenny** (her real asset library) | `66337f84-a0ab-4317-8848-95bab9cf9598` |
| Soul character (SFW anchor) | **Sunlit Café Moments** (`soul_2`) | `0dbb0542-6e08-4efc-9aa6-a27ebee184f9` |
| Soul character (NSFW anchor) | **Morning Velvet Elegance** (`soul_2`) | `0cf308ec-2b8a-4cc0-bc92-06850ed47f48` |

> Note: the empty project literally named "Jenny" (`5e214a73-...`) was a stray folder I
> created early on — the **real** library is the "Jenny" project `66337f84-...` above.

### Canonical face-reference images (reuse as character reference for new scenes)
These are the base images her existing content was built from:

| Role | Asset ID | URL |
|---|---|---|
| Face ref — mirror selfie | `f2b06ad3-7919-4024-91c6-4d9073e584ec` | https://d2ol7oe51mr4n9.cloudfront.net/user_34bYYUjyokjCcqRwbonTGH1zO9D/f2b06ad3-7919-4024-91c6-4d9073e584ec.png |
| Face ref — kitchen/robe | `52255acd-26a0-471f-9091-36624c1f16a1` | https://d2ol7oe51mr4n9.cloudfront.net/user_34bYYUjyokjCcqRwbonTGH1zO9D/52255acd-26a0-471f-9091-36624c1f16a1.png |
| Video start frame | `db976272-b477-47b4-acfa-afc90bdab515` | https://d2ol7oe51mr4n9.cloudfront.net/user_34bYYUjyokjCcqRwbonTGH1zO9D/db976272-b477-47b4-acfa-afc90bdab515.jpg |
| Faceswap char input | `6e267856-2409-49c5-9e32-1d9bb7ae78f1` | https://d2ol7oe51mr4n9.cloudfront.net/user_34bYYUjyokjCcqRwbonTGH1zO9D/6e267856-2409-49c5-9e32-1d9bb7ae78f1.png |

Local copies (gitignored) live in `reference_images/`.

## 4. The "slightly younger" adjustment (creator direction, 2026-10-15)

Add to prompts to nudge her a touch younger **while keeping the same person and clearly adult**:

> `early-twenties, youthful fresh-faced look, soft smooth (not aged) skin, bright rested eyes,
> naturally radiant — same identity and facial structure, unmistakably an adult woman in her
> early 20s`

Keep everything else in §1 identical. Do **not** change bone structure, eye color, or hair.

## 5. Where things live
- `CHARACTER_REFERENCE.md` — this file (how she looks + platform IDs).
- `ASSETS.md` — running catalog of everything generated (images + videos).
- `reference_images/` — local copies of key reference stills (gitignored).
