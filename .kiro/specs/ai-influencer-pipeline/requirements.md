# Requirements Document

## Introduction

This feature turns comfyui-vastai into the production base for one AI Instagram influencer: a fictional, clearly adult persona (25 or older). The persona is designed once, locked with trained character LoRAs, and produced in one Vast.ai session per week (target: 50–100 stills and 10–15 clips) on a single large-memory NVIDIA GPU: 80 GB minimum, sized for 141–192 GB cards such as the H200 or B200. Posts are then published by hand with Instagram's AI labels.

The repo keeps its conventions: one catalog, pipe-delimited config lists with `wf:` tags, idempotent and parallel-safe scripts, per-workflow installs on the stock Vast ComfyUI template, and tokens only in environment variables. The feature adds:

- catalog ids and config lines
- API-format render templates
- a Python batch tool that drives the local ComfyUI and Ollama
- an export and QC tool
- a LoRA training kit around ostris/ai-toolkit
- offline and online checks, with tests
- docs

### Verified context (28 Sep 2026)

- **ComfyUI port.** On the stock template ComfyUI listens on port 18188. Vast's proxy on 8188 needs Vast's token (REFERENCE.md §1). Local clients therefore default to `http://127.0.0.1:18188`.
- **Outputs.** ComfyUI writes to `/workspace/ComfyUI/output`, which `save-progress.sh` already copies with rclone.
- **Stock template only.** The golden image and the python3.11 shim were retired in v6, and README.md no longer mentions them.
- **Unpushed v6.** Local branch `v6` is 10 commits ahead of `origin/main`. `vast-provision.sh` clones `main`, so an instance gets this feature only after v6 and the feature are pushed.
- **The Mac.** It has bash 3.2, Python 3.9 (system), 3.11 and 3.14. PyYAML, pytest, hypothesis and shellcheck are not installed.
- **DeGrid order.** The [DeGrid README](https://github.com/lunaaispace-eng/ComfyUI-DeGrid) puts its node straight after VAE Decode, before any resize or upscaler. After an upscaler there is nothing left to remove. This reverses the earlier "upscale, then DeGrid" order.
- **Instagram limits.**
  - 5 hashtags per post and Reel since December 2025 ([Digital Trends](https://www.digitaltrends.com/social-media/instagram-now-limits-you-to-five-hashtags-per-post/)).
  - Captions up to 2,200 characters.
  - Carousels up to 20 items when posted from the app ([SocialRails](https://socialrails.com/blog/instagram-carousel-posts-guide)).
  - Content was rephrased for compliance with licensing restrictions.
- **Licences of the model sources.** Each was checked on its source page and API on 28 Sep 2026.
  - Qwen-Image 2.1 is under the Qwen Research License, which allows non-commercial (research or evaluation) use only ([model card](https://huggingface.co/Qwen/Qwen-Image-2.1), [LICENSE](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/LICENSE)). Comfy-Org's repack, which the `qwen21` and `qwen21-edit` lines install, declares the same licence.
  - Apache-2.0: Qwen-Image-Edit-2509 and [Qwen-Image-Edit-2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511), Comfy-Org's Qwen-Image and Qwen-Image-Edit repacks, and the 2511 Lightning LoRA.
  - Krea 2 Raw and Turbo, and Comfy-Org's Krea-2 repack, are under the [Krea 2 Community License](https://krea.ai/krea-2-licensing) v1 of 22 June 2026:
    - commercial use only while total annual revenue, affiliates included, is under US$1,000,000
    - content filters required (manual human review counts as one)
    - AI disclosure wherever law or platform policy requires it
    - no seat limit
  - The [Krea 2 Acceptable Use Policy](https://www.krea.ai/krea-2-use-policy) bans non-consensual intimate imagery, deceptive deepfakes of real people, content that is obscene under applicable law, circumventing Krea's safety measures, and presenting outputs as human-made to deceive. The circumvention ban puts the Krea2 Filter Bypass [Fedor] LoRA of `influencer-nsfw` in question. This draft keeps that LoRA listed as an open decision.
  - Civitai lists no `Image` commercial use for three Appendix A LoRAs: Aesthetic Amateur V4, the Smartphone Photography Slider and SNOFS.
  - Content was rephrased for compliance with licensing restrictions.

### Decisions in this draft (change any of them in review)

1. **Catalog ids.** `influencer` is part of `all`; `influencer-video`, `influencer-nsfw` and `character-design` are opt-in (`in_all=no`). Existing ids and `global` install exactly what they install today.
2. **NSFW add-on.** NSFW adapters form a separate opt-in add-on id, `influencer-nsfw`. They install under `models/loras/nsfw/` and are blocked from Instagram posts.
3. **File formats and a reviewed prompt sheet.** The Persona_File and Prompt_Sheet are YAML; Content_Calendars are YAML or CSV, so a week can be planned in a spreadsheet. The local Ollama writes image prompts, captions and alt text into a prompt sheet, and nothing renders until the user has reviewed it. Planning also runs on the Mac against a local Ollama, so writing uses no GPU time.
4. **Training.** Krea 2 Raw character-LoRA training is supported. MiniMax H3 character-LoRA training ships only as an experimental example, because it is unverified on one 80 GB GPU.
5. **Carousel edits.** Carousel slides 2 to N use Qwen-Image-Edit-2511 (Apache-2.0). ComfyUI core supports it from v0.5.0, Comfy-Org ships it in bf16 with an official workflow template, and its model card reports better character consistency than 2509. Qwen-Image 2.1 (the `qwen21` and `qwen21-edit` weights) is research-licensed and not used. Krea 2 Identity Edit and Krea2-Reference stay commented out until their LoRA hosts are confirmed.
6. **wan-animate.** Motion transfer stays interactive in the existing graph. The export tool finishes those renders.
7. **Full-precision weights.** The stills, edit and finishing models load bf16 weights rather than fp8 or int8 files, because GPU memory no longer limits the choice. These are Krea 2 Raw, Qwen-Image-Edit-2511 with its Qwen2.5-VL-7B text encoder, and the Qwen3-VL-4B text encoder.
8. **Commercial-use licence gate.** Every models.list and ollama-models.txt line tagged with an Influencer_Id records its licence. A line fails the gate when its licence does not permit commercial use of outputs. The offline Config_Checker (Requirement 26, criterion 18) and the online Source_Checker (Requirement 24, criterion 3) both check this. Licences with conditions pass and are reported with them. Resources that fail stay commented out with the reason (Requirement 2, criterion 6).

### Delivery phases

| Phase | Scope | Requirements |
|---|---|---|
| 1. Stills | `influencer` id, persona, calendar, planning, rendering, finishing, export, QC, Instagram separation rules, disk estimates, security, checks, docs | 1–15, 21 (criteria 2–4 and 6–8), 23, 25–28 |
| 2. Identity | Training kit, datasets, LoRA test grid | 17–20 |
| 3. Video | `influencer-video` id, Reels | 16 |
| 4. Optional | `influencer-nsfw` install, `character-design`, online source check | 21 (criteria 1 and 5), 22, 24 |

### Out of scope

- Stage 1 character design with closed models (Nano Banana Pro, GPT-Image-2.5), and publishing on Instagram. Both stay manual; the Runbook covers them.
- Automatic posting, scheduling, DMs and analytics.
- Automated face-similarity scoring. Common face-embedding models are licensed for non-commercial research only, so QC stays visual.
- Batch automation of wan-animate.
- Stopping the instance automatically: that needs calls to Vast's API from the instance, which breaks the local-only rule.
- Real-person likeness, face/head/body swaps, minor-coded resources and age sliders, anywhere in the repo.

## Glossary

- **Repo**: this repository, `iamwicked/comfyui-vastai`.
- **Instance**: a Vast.ai instance on the stock ComfyUI template, with the Repo at `/workspace/comfyui-vastai`.
- **Provisioner**: `scripts/provision.sh` and its front ends: `install-workflow.sh`, `stock-setup.sh`, `vast-provision.sh` and `common.sh`.
- **Catalog**: `config/catalog.txt`.
- **Config_Lists**: `config/models.list`, `config/nodes.txt` and `config/ollama-models.txt`.
- **Existing_Ids**: the Catalog ids that exist before this feature: `qwen21`, `qwen21-edit`, `krea2`, `edit-2509`, `krea-style`, `wan-i2v`, `minimax-h3`, `wan-animate`, `global` and `flux`.
- **Influencer_Ids**: the new Catalog ids: `influencer`, `influencer-video`, `influencer-nsfw` and `character-design`.
- **Selection**: the files, node packs and Ollama models that the Provisioner picks from the Config_Lists for one run (one id, `global`, or a full run).
- **Selection_Snapshot**: a committed record of the v6 Selection for every Existing_Id, for `global` and for a full run.
- **NSFW_Set**: the models.list lines tagged `influencer-nsfw`.
- **Instagram_Allowlist**: the LoRAs that an Instagram Post may load: the Character_LoRAs, plus the LoRAs of active lines tagged `influencer` or `influencer-video`, as Requirement 21, criterion 7 defines them.
- **ComfyUI_Env**: the Python environment ComfyUI runs in (`/venv/main` on the Instance).
- **ComfyUI_API**: ComfyUI's HTTP API, `http://127.0.0.1:18188` unless an environment variable overrides it.
- **Ollama_API**: Ollama's HTTP API, `http://127.0.0.1:11434`.
- **Persona**: a fictional character aged 25 or older, published as one Instagram account.
- **Persona_File**: the private YAML file for one Persona. It holds the slug, name, age, niche, voice, themes, locations (each with a description and optional Location_Shots), wardrobe, signature items, colour grade, Identity_Paragraph, Trigger_Word, Character_LoRAs, look-LoRA stacks, Hero_Image, Reference_Pack, caption rules and one Ollama model per platform.
- **Persona_Example**: the committed example Persona_File, describing a fictional adult Persona.
- **Identity_Paragraph**: the one fixed paragraph that describes the Persona's appearance.
- **Trigger_Word**: the token that activates the Persona's Character_LoRAs.
- **Character_LoRA**: a LoRA trained on the Persona. There is a stills LoRA for Krea 2 Raw and, optionally, a video LoRA for MiniMax H3.
- **Hero_Image**: the Persona's canonical reference image.
- **Reference_Pack**: up to 5 Persona images used for H3 reference-to-video.
- **Influencer_Root**: the folder with one Persona's private inputs and state. The default is `/workspace/influencer/<slug>/` on the Instance, outside the Repo checkout.
- **Output_Root**: `/workspace/ComfyUI/output/influencer/<slug>/`, which holds renders and exports.
- **Week_Id**: an ISO week, for example `2026-W40`.
- **Content_Calendar**: the YAML or CSV file that lists the Posts of one Week_Id.
- **Post**: one Content_Calendar entry. Its type is `single`, `carousel`, `story` or `reel`, and its platform is `instagram` (the default) or `other`.
- **Instagram Post**: a Post whose platform is `instagram`, the default when a Post names none.
- **Slide**: one image of a Post. Single and story Posts have one Slide.
- **Candidate**: one of several renders of one Slide.
- **Pick**: the Candidate chosen for finishing.
- **Reel**: the video of one reel Post: rendered with the Reel_Template or Ref_Reel_Template, or made outside the Batch_Generator and given to the Export_Tool (Requirement 13, criterion 11).
- **Prompt_Sheet**: the per-week YAML file with the generated prompts, captions, alt texts and Picks. The user reviews and edits it.
- **Approval**: the user's per-Post mark in the Prompt_Sheet that allows the render, finish and reels stages to run that Post.
- **Batch_Generator**: the Python tool with the stages `plan` (Content_Calendar to Prompt_Sheet) and `render`, `finish`, `reels`, `refine` and `grid` (Jobs on the ComfyUI_API).
- **Render_Template**: an API-format ComfyUI workflow with named placeholders, plus metadata: placeholder types, node-class-to-pack map, model files, identity flag, sizes and timeout. The kinds are:
  - Still_Template, Edit_Template, Finish_Template and Refine_Template, for stills. The Edit_Template edits carousel Slides with the Qwen-Image-Edit-2511 model listed in Appendix A (Requirement 11).
  - Reel_Template (H3 image-to-video) and Ref_Reel_Template (H3 reference-to-video), for Reels
- **Template_Filler**: the Batch_Generator part that fills a Render_Template.
- **Prompt_Assembler**: the Batch_Generator part that builds final prompts from the Persona_File, the Post and the LLM text.
- **Job**: one prompt submitted to the ComfyUI_API.
- **Job_Key**: a hash of a Job's filled prompt and the content of its input files.
- **Manifest**: a Job state file: the per-week file for Post Jobs, the per-Persona file for grid Jobs, or the per-Dataset-version file for refine Jobs, kept in the refined Dataset version's folder. For every Job it records the Job_Key, status, seed, ComfyUI prompt id, duration and outputs.
- **Safety_Guard**: the check that rejects text containing either of these (whole word, any letter case):
  - a term from the committed minor-coded term list
  - an age under 25 written as a number with an age word (for example "19 year old", "22yo")
- **Export_Tool**: the Python tool that turns finished renders into Instagram-ready files, QC_Sheets and Publish_Sheets.
- **Feed_Size**: 1080×1440 (3:4, the default) or 1080×1350 (4:5).
- **Vertical_Size**: 1080×1920 (9:16), for stories and Reels.
- **QC_Sheet**: a contact sheet that places renders beside the Hero_Image.
- **Publish_Sheet**: a per-Post file with the files to upload in order, the caption, the alt texts and the disclosure checklist.
- **H3_Rules**: MiniMax H3 limits from REFERENCE.md: 24 fps, a frame count of 17n+5, width and height in multiples of 32, and a pixel area of at most 768×1344.
- **Training_Kit**: the scripts that install ostris/ai-toolkit, check and caption Datasets, write training configs, run training and publish checkpoints.
- **Training_Env**: ai-toolkit's own Python environment on the `/workspace` volume.
- **Dataset**: a versioned folder of Persona images with caption files, `Influencer_Root/dataset/<version>/`.
- **Location_Shots**: empty shots (no person) of each recurring location, kept outside the Dataset and used as edit and Reel references.
- **Test_Grid**: 20 fixed test prompts with fixed seeds, rendered for each Character_LoRA checkpoint and strength.
- **Disk_Estimator**: the tool that reports download sizes per id and for training base models.
- **GB**: 10⁹ bytes, the unit every new tool reports (the Provisioner's existing free-space line is unchanged).
- **Source_Checker**: the online check that every source in the Config_Lists resolves and is free.
- **Config_Checker**: the offline check of the Catalog, Config_Lists, patches, Render_Templates and example files.
- **Noncommercial_List**: the committed list of licence identifiers whose terms do not permit commercial use of outputs, such as research-only and non-commercial licences (for example `qwen-research`). An identifier is a Hugging Face `license` value, or the `license_name` value when `license` is `other`.
- **Licence_Gate**: the commercial-use rule for lines whose `wf:` tag lists an Influencer_Id.
  - A line fails when its licence is on the Noncommercial_List, or when its Civitai commercial-use permissions do not include `Image`.
  - A licence that permits commercial use of outputs with conditions passes, and its conditions are reported. Conditions include revenue or seat ceilings, attribution and territories.
- **Readers and Printers**: the parse and print pairs:
  - Persona_Reader / Persona_Printer
  - Calendar_Reader / Calendar_Printer
  - Prompt_Sheet_Reader / Prompt_Sheet_Printer
  - Manifest_Reader / Manifest_Printer
  - LUT_Reader / LUT_Printer (3D `.cube` files)
  - Line_Reader / Line_Printer (single Catalog and Config_Lists lines)
- **Check_Suite**: one command that runs the Config_Checker, the Python tests, `bash -n` and shellcheck.
- **Runbook**: the influencer sections of README.md and REFERENCE.md.
- **Backup_Script**: `scripts/save-progress.sh`.

## Requirements

### Requirement 1: Catalog ids that leave existing installs unchanged

**User Story:** As the repo owner, I want the pipeline behind its own workflow ids, so that my existing workflows install exactly what they install today, and `all` and hands-off boots add only the stills pipeline.

#### Acceptance Criteria

1. THE Catalog SHALL list each Influencer_Id exactly once: `influencer` with `in_all` set to `yes`, on the line directly above the `global` line, and `influencer-video`, `influencer-nsfw` and `character-design` with `in_all` set to `no`.
2. THE Catalog SHALL set `min_comfyui` for `influencer-video` to `0.30.0`, the minimum of `minimax-h3`.
3. THE Catalog SHALL give `influencer-nsfw` a description that contains the word `add-on` and names `influencer` and `influencer-video` as separate ids.
4. WHEN the Provisioner runs for an Existing_Id, for `global` or as a full run, THE Provisioner SHALL select exactly the following Config_Lists lines, where the Selection_Snapshot records the Selections made at the `v6` commit that is the parent of this feature's first commit:
   - for an Existing_Id or `global`: the lines that the Selection_Snapshot records for that run
   - for a full run: the union of the lines that the Selection_Snapshot records for a full run and the lines of the `influencer` Selection

   Order SHALL NOT matter, and only `wf:` tags MAY differ.
5. WHEN `install-workflow.sh <id>` exits with status 0 for `influencer`, `influencer-video` or `character-design` on an Instance where no other Catalog id has been installed (including `global`), THE Provisioner SHALL have installed the following:
   - the id's UI workflows, meaning the files of the id's models.list `workflow` lines
   - every model file, node pack outside ComfyUI core and Ollama model that those UI workflows or the id's Render_Templates reference
6. WHEN the read-only Selection mode runs for one Catalog id, for `global` or for a full run, THE Provisioner SHALL print on standard output the Selection that the Provisioner makes when it installs that same id, `global` or full run. The output SHALL have one line per selected Config_Lists line, without its `wf:` tag, grouped under the name of its Config_Lists file and in file order.
7. WHEN the read-only Selection mode runs for the same id, `global` or full run from the same Repo commit, under the Instance's bash and under bash 3.2 on macOS, THE Provisioner SHALL exit with status 0 and print byte-identical standard output in both. The macOS run SHALL have only macOS's own command-line tools on the PATH and no `/workspace` folder.
8. WHEN `stock-setup.sh all` runs, or `vast-provision.sh` runs with `WORKFLOWS` set to `all`, THE Provisioner SHALL run the per-id install for exactly these ids, in this order: `qwen21`, `qwen21-edit`, `krea2`, `edit-2509`, `krea-style`, `wan-i2v`, `minimax-h3`, `wan-animate`, `influencer` and `global`.
9. IF the read-only Selection mode is given an id that the Catalog does not list, THEN THE Provisioner SHALL print an error that names the id and lists the Catalog ids, print no Selection, and exit with a non-zero status.
10. WHILE the read-only Selection mode runs, THE Provisioner SHALL open no network connection and SHALL create, change or delete no file or directory outside the system temporary folder.

### Requirement 2: Pinned, free and confirmed sources

**User Story:** As the repo owner, I want every new download pinned to a confirmed, free source, so that installs are reproducible and never hit a paywall or an unofficial mirror.

#### Acceptance Criteria

1. THE Config_Lists SHALL reference the file of each active `civitai` line whose `wf:` tag lists only Influencer_Ids by a model version id (the part before any `::member`). On the verification date recorded above the line, a Civitai account without a paid membership SHALL have been able to download that version with its CIVITAI_TOKEN, paying nothing for the file or for early access.
2. THE Config_Lists SHALL reference the file of each active `hf` line whose `wf:` tag lists only Influencer_Ids by a `repo::path` source id that writes out both the repository id and the file's path inside the repository, also when that path equals the filename.
3. THE Config_Lists SHALL pin each active `workflow` line whose `wf:` tag lists only Influencer_Ids and whose source is a `github.com` or `raw.githubusercontent.com` URL to a full 40-character hexadecimal commit SHA in that URL, never to a branch or tag name.
4. THE Config_Lists SHALL record, in the comment block directly above each active, non-`bundled` line whose `wf:` tag lists only Influencer_Ids (for a group of consecutive such lines from one source page, above the first of them):
   - the source page as an `https://` URL
   - as a `# licence:` line, the licence or Civitai usage permissions that the source page states (`none stated` when it states none), with any conditions that the licence sets on commercial use of outputs
   - the date the source was verified, in `YYYY-MM-DD` form

   THE Config_Lists SHALL also record a `# licence:` line of the same form in the comment block directly above each active models.list and ollama-models.txt line whose `wf:` tag lists an Influencer_Id together with another id (for a group of consecutive such lines from one source page, above the first of them).
5. THE Config_Lists SHALL point each active, non-`bundled` line whose `wf:` tag lists only Influencer_Ids at an official download host of its resource. An official download host is a repository or model page of the person or organisation that first published the resource, a repository or model page of Comfy-Org, or a location that the first publisher's model card, model page or README links to.
6. WHILE the resource of a line whose `wf:` tag lists only Influencer_Ids has no confirmed official download host, or its licence fails the Licence_Gate, THE Config_Lists SHALL hold that line commented out with a leading `#`, directly below a comment that states the reason. As of 2026-09-28 this covers:
   - no confirmed host: the Krea 2 Identity Edit LoRA and the Krea2-Reference LoRA
   - Licence_Gate failure: the Aesthetic Amateur V4, Smartphone Photography Slider and SNOFS LoRAs
7. IF Civitai answers a download with HTTP 403 and a response body that contains "early access" in any letter case (an early-access answer), THEN THE Provisioner SHALL print a warning that names the line's filename and version id and gives early access as the cause, and continue with the next line.
8. IF Civitai gives an early-access answer, THEN THE Provisioner SHALL discard the answer and keep no file from it on disk, including at the line's install path.
9. IF a Provisioner run has received one or more early-access answers, THEN THE Provisioner SHALL end the run with a non-zero exit status and a closing problem summary that names each affected filename and version id.

### Requirement 3: Stills install (`influencer`)

**User Story:** As the creator, I want one command that installs the Krea 2 realism stack, the carousel edit model and the finishing nodes, so that a fresh instance can render a week of stills.

#### Acceptance Criteria

1. WHEN `install-workflow.sh influencer` exits with status 0 on an Instance, including a fresh Instance on which `stock-setup.sh init` has not run, THE Provisioner SHALL have installed every file, node pack and Ollama model that Appendix A lists for `influencer`. Each model file and workflow file SHALL exist with a non-zero size in its target folder (for a `.zip` workflow, its extracted files). Each node pack SHALL exist in ComfyUI's `custom_nodes` folder, and `ollama list` SHALL show each Ollama model.
2. THE Config_Lists SHALL add `influencer` to the `wf:` tag of these existing lines: the Lenovo Krea 2 LoRA, the Qwen-Image VAE (`qwen_image_vae.safetensors`, the VAE that Qwen-Image-Edit-2511 uses), RES4LYF, rgthree and `qwen3:8b`. Each tag SHALL keep every id it already lists (`global` for the untagged `qwen3:8b` line), so that no line tagged `influencer` installs a file path, node pack or Ollama model that another active line also installs. The bf16 Krea 2 Raw, Qwen-Image-Edit-2511 and Qwen2.5-VL-7B files that Appendix A lists SHALL each have a new line tagged only `influencer`.
3. WHEN `install-workflow.sh influencer` runs, THE Provisioner SHALL download the model weights that a node pack selected for `influencer` would otherwise download on first use (for ComfyUI-VOSR2, its weights from HF `CSWRY/VOSR`) into the folder that the node pack reads, so that the first Job using that node pack downloads no file.
4. IF a folder named `ComfyUI-SaveSimple`, in any letter case, exists directly inside ComfyUI's `custom_nodes` folder, THEN THE Provisioner SHALL skip ComfyUI-DeGrid (no clone, update or requirements install) and report the collision on the `VAEDeGrid` node id, with the path of the ComfyUI-SaveSimple folder.
5. WHEN `install-workflow.sh` exits with status 0 for an Influencer_Id, THE Provisioner SHALL have installed every model file that a node in the id's UI workflows names, at that relative path (subfolder included) in the model folder the node reads. The file SHALL carry either the name the node uses, or an installed name that a patch in `config/patches/` has written into the node. The id's UI workflows are the workflow files that its models.list lines install, including each file extracted from a `.zip` archive. Nodes inside subgraphs and muted or bypassed nodes count.
6. IF installing an item of the `influencer` Selection fails (download, clone, requirements, patch or Ollama pull), or ComfyUI-DeGrid is skipped under criterion 4, THEN THE Provisioner SHALL try every remaining item, list each failed or skipped item when the run ends, and exit with a non-zero status.
7. WHEN `install-workflow.sh influencer` runs again after a run that exited with status 0, with the Config_Lists and the installed items unchanged, THE Provisioner SHALL exit with status 0 without downloading any model or workflow file, cloning any node pack or pulling any Ollama model.

### Requirement 4: Persona file

**User Story:** As the creator, I want one private persona file as the single source of truth for identity, voice and look, so that prompts, captions, training configs and exports all use the same values.

#### Acceptance Criteria

1. THE Repo SHALL include a Persona_Example that describes a fictional Persona aged 25 or older, gives every Persona_File field a non-empty value (including optional fields such as the video Character_LoRA and the disclosure line), and passes every check of this requirement with no error and no warning, except the criterion 7 check that the named files exist.
2. IF a Persona_File that the Batch_Generator, Export_Tool or Training_Kit loads fails one or more checks of this requirement, THEN THE Persona_Reader SHALL report all failures together, not only the first. Each failure SHALL include the file path, the field (with the item number for a list item) or, for a YAML syntax error, the line number, and the reason.
3. IF the age of a Persona_File is missing, is not a YAML integer, or is below 25 or above 99, THEN THE Persona_Reader SHALL reject the Persona_File.
4. IF any string in a Persona_File, including list items and mapping keys at any depth, contains text that the Safety_Guard rejects, THEN THE Persona_Reader SHALL reject the Persona_File and name the field and the matched term.
5. IF the Trigger_Word is shorter than 4 characters, is longer than 32 characters, or contains a character other than ASCII lowercase letters (a to z), digits (0 to 9) and underscores, THEN THE Persona_Reader SHALL reject the Persona_File.
6. IF the slug is longer than 32 characters or contains a character other than ASCII lowercase letters (a to z), digits (0 to 9) and hyphens, THEN THE Persona_Reader SHALL reject the Persona_File.
7. IF the path of the Hero_Image, of a Reference_Pack image or of the LUT in a Persona_File is absolute, contains a `..` segment, or does not lead to an existing file at its resolved path (a folder does not count), THEN THE Persona_Reader SHALL reject the Persona_File and name the field and the path.
8. IF the Reference_Pack of a Persona_File lists more than 5 images (0 to 5 are valid), THEN THE Persona_Reader SHALL reject the Persona_File and report the number of images listed.
9. IF a Persona_File defines fewer than 3 or more than 4 locations, THEN THE Persona_Reader SHALL print a warning that states the count, without rejecting the Persona_File.
10. THE Persona_Reader SHALL resolve each Hero_Image, Reference_Pack image and LUT path of a Persona_File to the Influencer_Root joined with that path, regardless of the current working directory, so that the same Persona_File works without edits on the Mac and on the Instance.
11. THE Persona_Printer SHALL write every valid Persona_File so that the Persona_Reader reads back the same field values, including multi-line text and non-ASCII characters such as emoji. Comments, key order and quoting may differ (round-trip property).
12. IF a Persona_File does any of the following, THEN THE Persona_Reader SHALL reject the Persona_File and name each field involved:
    - lacks the slug, name, voice, Identity_Paragraph, Trigger_Word, Hero_Image or LUT
    - leaves one of those fields empty or only whitespace
    - gives a field a type other than the one the Persona_File format defines (for example text where a list is expected)
    - contains a field that the Persona_File format does not define
13. IF two locations in a Persona_File, or two outfits in its wardrobe, have the same name, THEN THE Persona_Reader SHALL reject the Persona_File and report the repeated name.
14. IF the Persona_Reader rejects a Persona_File, THEN THE Batch_Generator, Export_Tool or Training_Kit that loaded it SHALL exit with a non-zero status. It SHALL send no request to the ComfyUI_API or the Ollama_API and create or change no file.

### Requirement 5: Content calendar

**User Story:** As the creator, I want to plan a week of posts in one calendar file, so that one set of commands produces the whole week.

#### Acceptance Criteria

1. THE Calendar_Reader SHALL accept Posts of type `single`, `carousel`, `story` and `reel`, each on platform `instagram` or `other` (`instagram` when the Post names no platform).
2. IF a Post names no type, a type other than `single`, `carousel`, `story` or `reel`, or a platform other than `instagram` or `other`, THEN THE Calendar_Reader SHALL reject the Post, name the Post id and the field, and list the accepted values for that field.
3. IF a Content_Calendar's Week_Id is missing, does not match `YYYY-Www`, or has a week `ww` outside 01 to the number of ISO weeks in year `YYYY` (52 or 53, so `2026-W53` passes and `2025-W53` fails), THEN THE Calendar_Reader SHALL reject the Content_Calendar and name the value or report that it is missing.
4. IF a Post id is missing or empty, is longer than 48 characters, contains a character other than a lowercase letter a–z, a digit 0–9 or a hyphen, is `other` (the folder that Requirement 10 criterion 11 uses for platform `other` Posts), or is also the id of another Post in the Content_Calendar, THEN THE Calendar_Reader SHALL reject the Content_Calendar and name each offending id, or the list position of each Post that has none.
5. IF a Post names a location that is not one of the Persona_File's location names, or an outfit that is not one of its outfit names (compared exactly, including letter case), THEN THE Calendar_Reader SHALL reject the Post, name the Post id and the unknown name, and list the names that the Persona_File defines for that field.
6. IF a carousel Post asks for a number of Slides that is not a whole number from 2 to 20, THEN THE Calendar_Reader SHALL reject the Post and name the Post id and the requested number.
7. IF a reel Post asks for a duration that is not a number of seconds from 5 to 10 inclusive (fractions such as 7.5 pass), THEN THE Calendar_Reader SHALL reject the Post and name the Post id and the requested duration.
8. IF a reel Post takes its first frame from a Post id that the Content_Calendar does not contain, or from a Slide number outside 1 to that Post's Slide count, THEN THE Calendar_Reader SHALL reject the reel Post and name its id and the unresolved reference.
9. WHEN the Calendar_Reader rejects a Post, THE Calendar_Reader SHALL still accept each other Post of that Content_Calendar that passes its own checks, including a reel Post that takes its first frame from the rejected Post.
10. THE Batch_Generator SHALL place every output path inside the Output_Root for every Week_Id and Post id that the Calendar_Reader accepts (path-containment property).
11. WHEN the user asks for a new Content_Calendar for a Week_Id that passes criterion 3 and whose Content_Calendar file does not exist yet, THE Batch_Generator SHALL write a skeleton Content_Calendar for that Week_Id that has at least one Post, names only locations and outfits that the Persona_File defines, and passes the Calendar_Reader with no rejected Post.
12. IF the user asks for a new Content_Calendar for a Week_Id that fails criterion 3, or whose Content_Calendar file already exists, THEN THE Batch_Generator SHALL write no file and report the invalid Week_Id or the path of the existing file.
13. THE Calendar_Printer SHALL write every Content_Calendar that the Calendar_Reader accepts with no rejected Post so that the Calendar_Reader reads back the same Week_Id and the same Posts, in the same order and with equal field values (round-trip property).
14. THE Calendar_Reader SHALL read a Content_Calendar from a `.yaml` or `.yml` file, or from a `.csv` file in the CSV format that the Runbook documents: UTF-8 text (with or without a byte-order mark), one header row of column names, and one row per Post. THE Calendar_Reader SHALL read each CSV Post's mode from a mode column that takes the same values as the YAML format (`regenerate`, `reference` and `external`, accepted per Post type as criterion 20 states), and SHALL read an empty mode cell as no mode. THE Calendar_Reader SHALL skip CSV rows in which every cell is empty.
15. IF a CSV Content_Calendar lacks a column that the CSV format requires, has a column that the CSV format does not define, names a column more than once, or has rows whose Week_Ids differ, THEN THE Calendar_Reader SHALL reject the Content_Calendar and name each such column, or give the row number (counting the header row as row 1, as a spreadsheet shows it) of each row whose Week_Id differs from that of the first Post row.
16. THE Calendar_Reader SHALL give equal results for a CSV file and a YAML file that hold the same Week_Id and the same Posts, in the same order and with equal field values: equal Content_Calendars with the same rejected Posts, or the rejection of both Content_Calendars (equivalence property).
17. WHEN the user asks for a new Content_Calendar in CSV form under the conditions of criterion 11, THE Batch_Generator SHALL write the skeleton Content_Calendar of criterion 11 as a CSV file.
18. THE Calendar_Printer SHALL write every Content_Calendar that the Calendar_Reader accepts with no rejected Post as a CSV file, so that the Calendar_Reader reads back the same Week_Id and the same Posts, in the same order and with equal field values (round-trip property). This SHALL hold for text that contains commas, double quotes, line breaks and non-ASCII characters such as emoji.
19. IF more than one Content_Calendar file exists for a Week_Id (for example a `.yaml` and a `.csv` file), THEN THE Batch_Generator and the Export_Tool SHALL name every such file and exit with a non-zero status, without sending any request to the Ollama_API or the ComfyUI_API and without creating or changing any file.
20. IF a Post sets a mode that the Post's type does not accept, THEN THE Calendar_Reader SHALL reject the Post, name the Post id and the mode, and list the modes that the type accepts. Carousel Posts accept only `regenerate`, reel Posts accept only `reference` and `external`, and single and story Posts accept no mode.

### Requirement 6: Prompts, captions and alt text from the local LLM

**User Story:** As the creator, I want the local Ollama to draft each post's image prompts, caption and alt text in the persona's voice, so that I review and edit instead of writing from scratch.

#### Acceptance Criteria

1. WHEN the plan stage runs for a Week_Id, THE Batch_Generator SHALL write, for every Post in the Week_Id's Content_Calendar that has no Prompt_Sheet entry or that the user names for re-planning, a Prompt_Sheet entry with one scene text per Slide, one caption, hashtags, one alt text per Slide and, for reel Posts, one motion description, taken from the first Ollama_API response that passes validation.
2. THE Batch_Generator SHALL send each Post's request to the Ollama model that the Persona_File names for the Post's platform (default `qwen3:8b` for `instagram` and `dolphin3:8b` for `other`).
3. WHILE the plan stage runs, THE Batch_Generator SHALL send no request to the ComfyUI_API, so that planning works on the user's Mac against a local Ollama with no ComfyUI running.
4. IF an attempt for a Post counts as a failed validation, THEN THE Batch_Generator SHALL send the Post's request to the Ollama_API again, up to 3 more times (at most 4 attempts per Post in one run).
5. IF the Safety_Guard rejects any part of an LLM response, THEN THE Batch_Generator SHALL count the attempt as a failed validation without writing any part of that response to the Prompt_Sheet, not even as a kept raw response.
6. THE Prompt_Assembler SHALL build each prompt for a Render_Template flagged identity-required from four parts, in this order: the Trigger_Word, the Identity_Paragraph, the Persona_File's descriptions of the Post's location and outfit, and the Slide's scene text as recorded in the Prompt_Sheet.
7. THE Prompt_Assembler SHALL include the Identity_Paragraph verbatim and exactly once in every prompt for a Render_Template flagged identity-required, whatever the scene text, including empty scene text and scene text that already contains the Identity_Paragraph (invariant).
8. IF an Instagram Post's full caption text (the caption with its disclosure line, plus the hashtags and the separators between them) is longer than 2,200 characters counted as UTF-16 code units (an emoji such as 😀 counts as 2), or contains more words starting with `#` than the Persona_File's hashtag limit (default 5; a higher limit counts as 5), THEN THE Batch_Generator SHALL count the attempt as a failed validation.
9. WHERE the Persona_File defines a disclosure line, THE Batch_Generator SHALL end every caption with the disclosure line, on its own line and exactly once, so that adding the line to a caption that already ends with it leaves the caption unchanged (idempotence).
10. IF a caption or its hashtags contain a term from the Persona_File's flagged-terms list (for example a health claim), matched as whole words in any letter case, THEN THE Batch_Generator SHALL flag the Post for review in the Prompt_Sheet with each matched term, without changing the caption or failing the attempt.
11. WHEN the plan stage runs for a Week_Id that already has a Prompt_Sheet, THE Batch_Generator SHALL leave every value unchanged in each existing entry whose Post the user has not named for re-planning, including user edits, Picks and the needs-manual-prompt mark.
12. THE Prompt_Sheet_Printer SHALL write every valid Prompt_Sheet so that the Prompt_Sheet_Reader reads back an equivalent Prompt_Sheet: the same entries and fields, with every text value identical character for character (round-trip property). This SHALL hold for multi-line text, text containing `#` or `:`, text such as `no` or `2026` that YAML could read as another type, and non-ASCII characters such as emoji.
13. IF a response from the Ollama_API does not arrive in full within 600 seconds of the request, or does not hold exactly one non-empty scene text and one non-empty alt text per Slide, one non-empty caption and, for a reel Post, one non-empty motion description, THEN THE Batch_Generator SHALL count the attempt as a failed validation.
14. IF the 4th attempt for a Post counts as a failed validation, THEN THE Batch_Generator SHALL mark the Post in the Prompt_Sheet as needing a manual prompt, attaching the most recent raw response that the Safety_Guard did not reject (if any) and keeping any values the Post's entry already had.
15. IF the Ollama_API cannot be reached within 10 seconds, or reports that the model a Post needs is not installed, THEN THE Batch_Generator SHALL stop the plan stage with a non-zero exit status and an error that names the URL it tried or the missing model, keeping every Prompt_Sheet entry written before the stop.
16. WHEN the plan stage writes a new or re-planned Prompt_Sheet entry, THE Batch_Generator SHALL write the entry without Approval, also when criterion 14 keeps the entry's other values.
17. THE Batch_Generator SHALL leave the Approval of every Prompt_Sheet entry unchanged, except when criterion 16 writes the entry without Approval, so that only the user gives Approval, and a Post flagged for review or marked as needing a manual prompt stays without Approval until the user approves it.

### Requirement 7: Render templates and template filling

**User Story:** As the maintainer, I want render templates with typed placeholders and a filler that validates them, so that every Job sent to ComfyUI is complete and well-formed.

#### Acceptance Criteria

1. THE Repo SHALL provide these Render_Templates as API-format JSON (the node-graph format that the ComfyUI_API accepts as a prompt). Each SHALL come with metadata that declares a name and a type for every placeholder, and a minimum and a maximum for every integer or float placeholder and every LoRA strength. For a seed placeholder the bounds SHALL be 0 to 4,294,967,295.
   - for `influencer`: the Still_Template, Edit_Template, Finish_Template and Refine_Template
   - for `influencer-video`: the Reel_Template and Ref_Reel_Template
2. WHEN the Template_Filler fills a Render_Template, THE Template_Filler SHALL replace every occurrence of every placeholder that the Render_Template contains and keep the rest of the Render_Template's JSON unchanged (node ids, node classes, input names and placeholder-free input values). Placeholder syntax inside a supplied value SHALL stay as supplied. The only exceptions are the loaders and links that criterion 5 adds for LoRA chains (invariant).
3. IF a placeholder that the Render_Template contains has no supplied value, or a supplied name matches no placeholder in the Render_Template, THEN THE Template_Filler SHALL return no filled prompt and report an error that names the Render_Template, every placeholder without a value and every supplied name that matches no placeholder.
4. WHEN the Template_Filler fills an integer, float, text or file-name placeholder that forms a whole input value, THE Template_Filler SHALL set the input to the supplied value unchanged. Integer and float values SHALL become JSON numbers, and text and file-name values SHALL become JSON strings (property, including text with quotes, backslashes, line breaks and emoji).
5. WHEN the Template_Filler fills a LoRA-list placeholder (an ordered list of 0 to 16 LoRA file names, each with a strength), THE Template_Filler SHALL insert one LoRA loader node per entry, in list order and with the entry's file name and strength, linked as follows, so that the filled prompt has no cycles and every link in it points to a node that exists in the filled prompt (invariant):
   - the first loader takes the source output that the Render_Template declares for the placeholder
   - each further loader takes the previous loader's output
   - the consumer inputs that the Render_Template declares for the placeholder take the last loader's output, or the source output when the list is empty
6. WHEN the Template_Filler fills the same Render_Template with equal values more than once, THE Template_Filler SHALL produce filled prompts that serialize to byte-identical JSON, including the node ids of inserted LoRA loaders. This SHALL hold whatever order the values are supplied in, and whether the fills run in one process or in separate processes, so that an unchanged Job keeps its Job_Key (property).
7. IF any of these holds, THEN THE Template_Filler SHALL return no filled prompt and report an error. The error SHALL name the Render_Template and, for each such value, the placeholder, the declared type or bounds, and the supplied value.
   - a supplied value does not match the type that the Render_Template metadata declares for its placeholder
   - a supplied integer, float or LoRA strength is below the declared minimum or above the declared maximum
   - a LoRA list has more than 16 entries

   The type rules are:
   - an integer placeholder takes only integers: no booleans, and no floats such as 1.0
   - a float placeholder takes integers and finite floats, but no booleans
   - text and file-name placeholders take only strings, and a file-name placeholder takes no empty string
8. WHEN the Template_Filler fills a text, file-name or integer placeholder that forms part of a longer string value, THE Template_Filler SHALL replace only the placeholder, with the supplied string or the supplied integer written in decimal, and keep every character around it unchanged (property).

### Requirement 8: Krea 2 stills with the realism recipe

**User Story:** As the creator, I want each still rendered with the community Krea 2 realism recipe plus my character LoRA, so that posts look like phone photos of the same person.

#### Acceptance Criteria

1. THE Still_Template SHALL use the Krea 2 Raw bf16 diffusion model and the bf16 Qwen3-VL-4B text encoder listed in Appendix A, and no other diffusion model or text encoder.
2. THE Still_Template SHALL apply exactly these LoRAs to the Krea 2 Raw model, in this order: the official Krea 2 turbo LoRA listed in Appendix A at strength 0.6; the stills Character_LoRA at the Persona_File's strength (default 1.0); then the Persona_File's stills look-LoRA stack for the Post's platform, in the listed order and at the listed strengths (1.0 for an entry without a strength).
3. WHERE a Post sets its own look list, THE Batch_Generator SHALL use that list, in its order and at its strengths, in place of the Persona_File's look-LoRA stack for every Still_Template Job of the Post, so that an empty list gives no look LoRAs.
4. WHERE the Persona_File does not enable sharp decoding (the default), THE Still_Template SHALL decode with the Wan 2.1 fp32 VAE listed in Appendix A.
5. THE Still_Template SHALL sample with the two RES4LYF passes of the realism guide workflow (Civitai version 3092832): the same sampler node types, chained as in the guide, with every sampler setting except the seed (such as sampler, scheduler, step count, CFG and denoise) equal to the guide's value, and the Job's seed in every pass that takes a seed.
6. WHEN the Batch_Generator renders a Slide with the Still_Template, THE Still_Template SHALL output an image of exactly the width and height that its metadata lists for the Post's aspect ratio (3:4 for single and carousel Posts, 4:5 when such a Post sets 4:5, 9:16 for story Posts). The width-to-height ratio of that pair SHALL equal the aspect ratio exactly.
7. WHERE the Persona_File enables sharp decoding, THE Still_Template SHALL decode with the Wan 2.1 upscale2x VAE listed in Appendix A through ComfyUI-VAE-Utils. After the grid removal of Requirement 12, it SHALL halve the decoded width and height, so that the output keeps the size set in criterion 6.
8. THE Still_Template's Render_Template metadata SHALL flag it identity-required, so that every Still_Template prompt in the render stage contains the Trigger_Word and the Identity_Paragraph (Requirement 6).
9. IF the render stage starts with a Persona_File that names no stills Character_LoRA, THEN THE Batch_Generator SHALL stop before submitting any Job and report that the Persona_File has no stills Character_LoRA.
10. IF the Persona_File's stills Character_LoRA strength is 0 or less or above 2.0, a look LoRA strength in the Persona_File or a Post is below -2.0 or above 2.0, or a look list names more than 8 LoRAs, THEN THE Batch_Generator SHALL reject the Persona_File or Post that holds the value and name the LoRA or list and the value.

### Requirement 9: Preflight checks and dry run

**User Story:** As the creator, I want each stage to check the instance before it spends GPU time and to show me the work it would do, so that a missing node or model fails in seconds instead of halfway through the batch.

#### Acceptance Criteria

1. IF a ComfyUI_API request made for the checks of criterion 2 is refused, is answered with an error or with a response that is not a ComfyUI_API response, or gets no answer within 10 seconds, THEN THE Batch_Generator SHALL submit no Job and report the URL it tried and which of these failures occurred.
2. WHEN a render, finish, reels, refine or grid stage starts, THE Batch_Generator SHALL, before submitting any Job, compare the node classes, input names and model files named in every Job that the stage would submit or hold as waiting (including the LoRAs filled in from the Persona_File, a Post or the grid stage's list of Character_LoRA checkpoints) with the node definitions and model lists that the ComfyUI_API reports.
3. IF the ComfyUI_API does not report a node class, input name or model file that criterion 2 compares, THEN THE Batch_Generator SHALL submit no Job and name each distinct missing item once. Each item SHALL come with its node pack (for a node class or input name) and the Catalog ids whose Selection includes it, or with a note that no Catalog id's Selection includes it (for example a Character_LoRA).
4. IF the file system that holds the Output_Root has less than 20 GB free at the start of a render, finish, reels, refine or grid stage, THEN THE Batch_Generator SHALL submit no Job and report the free space, the 20 GB minimum and the path it checked.
5. WHEN a render, finish, reels, refine or grid stage runs in dry-run mode, THE Batch_Generator SHALL write one JSON file for each Job that the stage would submit, without submitting any Job and without creating or changing the Manifest. Each file SHALL hold the same filled prompt that the stage would submit outside dry-run mode, in a form the ComfyUI UI can load, and the files SHALL go into a folder inside the Output_Root that holds only this dry run's files.
6. WHEN a render, finish, reels, refine or grid stage runs in dry-run mode, THE Batch_Generator SHALL report the folder of criterion 5 and, for each Render_Template, the number of Jobs it would submit, skip as done and hold as waiting, plus an estimated time. The estimated time SHALL be the median duration of the done Jobs of that Render_Template across all the Persona's Manifests, the current one included (for an even count, the mean of the two middle values), multiplied by the number of Jobs to submit and rounded up to whole minutes. It SHALL be `unknown` when no such duration is recorded.
7. WHILE the render stage runs outside dry-run mode, WHEN the checks of criteria 1 to 4 pass, THE Batch_Generator SHALL ask the Ollama_API to unload every model that the Ollama_API reports as loaded, before submitting the first Job.
8. IF any check of criteria 1 to 4 fails, THEN THE Batch_Generator SHALL report every failed check and exit with a non-zero status within 60 seconds of the stage start, without creating or changing the Manifest.
9. WHILE a stage runs in dry-run mode, IF any check of criteria 1 to 4 fails, THEN THE Batch_Generator SHALL still write the files of criterion 5 and the report of criterion 6 before it exits.
10. IF the Ollama_API refuses the connection or gives no answer within 10 seconds during the unload of criterion 7, THEN THE Batch_Generator SHALL print a warning with the Ollama_API URL and continue the stage.
11. WHILE a Post has no Prompt_Sheet entry, or its entry has no Approval, THE Batch_Generator SHALL submit no Job for that Post in the render, finish or reels stage, and SHALL report the Post as waiting for planning or as waiting for review, respectively.

### Requirement 10: Job tracking and resume

**User Story:** As the creator, I want the batch tracked Job by Job, so that I can start it, walk away, and re-run the same command after any interruption without paying for duplicate renders.

#### Acceptance Criteria

1. THE Batch_Generator SHALL derive the seed of each Job of a Post only from the Week_Id, Post id, Slide index and Candidate index, as an integer from 0 to 4,294,967,295, so that the same four values give the same seed in every run and on every machine, and record the seed in the Manifest.
2. THE Batch_Generator SHALL compute each Job_Key from the filled prompt and the bytes of every input image the Job loads (such as a Pick, a finished image, the Hero_Image or a Reference_Pack image), so that the same prompt and bytes give the same Job_Key in every run, and a change to any prompt value or input byte, such as a new Pick, gives a new Job_Key.
3. WHEN a stage reaches a Job whose Job_Key the Manifest records as done, and every output file that the Manifest records for that Job exists and is larger than 0 bytes, THE Batch_Generator SHALL skip the Job without submitting it to the ComfyUI_API.
4. WHILE an input image of a Job (a Pick or a finished image) does not exist, THE Batch_Generator SHALL hold the Job without submitting it, record it as waiting, and continue with the other Jobs of the stage.
5. IF the ComfyUI_API answers but a Job recorded as submitted is not queued, not running and not in the ComfyUI history, THEN THE Batch_Generator SHALL, within 30 seconds, resubmit the Job if it has been resubmitted fewer than 2 times in the current stage run, and otherwise record it as failed with an error indicating that ComfyUI lost it and continue with the remaining Jobs.
6. IF the ComfyUI_API rejects a Job or reports an execution error for it, THEN THE Batch_Generator SHALL record the Job as failed with ComfyUI's error message, not resubmit it in the same stage run, and continue with the remaining Jobs.
7. IF a Job is still executing when its timeout has passed, counted from the moment ComfyUI starts executing it, THEN THE Batch_Generator SHALL, within 30 seconds, ask the ComfyUI_API to interrupt the Job, record it as failed with an error indicating the timeout, and continue with the remaining Jobs. The timeout is the value in the Job's Render_Template metadata or, where the metadata sets none, 900 seconds for the Still_Template, Edit_Template, Finish_Template and Refine_Template and 3,600 seconds for the Reel_Template and Ref_Reel_Template.
8. WHEN a stage ends, meaning no Job of the stage is queued, running or ready for submission, THE Batch_Generator SHALL list each failed Job with its error and each waiting Job with the input image it lacks, naming each Job by Post id, Slide index and Candidate index, or for grid Jobs by Test_Grid prompt number, checkpoint and strength.
9. WHEN a Job's status changes to waiting, submitted, done or failed, THE Batch_Generator SHALL write the Manifest by atomically replacing the previous file before it submits another Job, so that a process killed at any moment leaves a Manifest that the Manifest_Reader reads without error.
10. WHILE a Batch_Generator process runs a stage for a Week_Id, THE Batch_Generator SHALL refuse to start a second process for the same Week_Id: the second process SHALL report the running process id and exit with a non-zero status before it submits any Job or writes the Manifest. A process that has exited or been killed SHALL not count as running.
11. THE Batch_Generator SHALL save the outputs of Posts with platform `instagram` under `Output_Root/<Week_Id>/<post-id>/`, and the outputs of Posts with platform `other` under `Output_Root/<Week_Id>/other/<post-id>/`.
12. THE Manifest_Printer SHALL write every valid Manifest so that the Manifest_Reader reads back an equal Manifest (round-trip property). This SHALL hold for error messages that contain line breaks, quotes and non-ASCII characters.
13. WHEN a stage reaches a Job whose Job_Key the Manifest records as submitted, and ComfyUI still has the Job's ComfyUI prompt id queued, running or in its history, THE Batch_Generator SHALL wait for that prompt and record its outputs or error for the Job without submitting the Job again.
14. WHEN a stage ends, THE Batch_Generator SHALL exit with status 0 if no Job of the stage failed, including when Jobs are waiting, and with a non-zero status if 1 or more Jobs failed.
15. IF the ComfyUI_API gives no answer within 10 seconds after the stage's preflight checks (Requirement 9) have passed, THEN THE Batch_Generator SHALL keep retrying for up to 300 seconds without changing any Job's status, and SHALL exit with a non-zero status and report the URL it tried if no answer arrives in that time.
16. IF the Manifest that a stage uses exists but the Manifest_Reader cannot read it, THEN THE Batch_Generator SHALL submit no Job, leave the file unchanged, report the path and the error, and exit with a non-zero status.

### Requirement 11: Carousels, Candidates and Picks

**User Story:** As the creator, I want carousel slides that keep slide 1's person, outfit and location, and a choice between candidates, so that every slide of a post looks like the same moment.

#### Acceptance Criteria

1. THE Batch_Generator SHALL render Slide 1 of a carousel Post that does not set mode `regenerate` with the Still_Template.
2. THE Batch_Generator SHALL render Slides 2 to N of a carousel Post that does not set mode `regenerate` (N is the Post's Slide count) with the Edit_Template.
   - The Edit_Template SHALL use the bf16 Qwen-Image-Edit-2511 diffusion model and the bf16 Qwen2.5-VL-7B text encoder listed in Appendix A, and no other diffusion model or text encoder.
   - Each of these Slides SHALL use the Pick of Slide 1 as its reference image.
   - Each of these Slides SHALL render at the width and height that the Edit_Template's metadata lists for that Pick's aspect ratio (3:4, or 4:5 when the Post sets 4:5).
   - The width-to-height ratio of that pair SHALL equal the aspect ratio exactly.
   - The pair's area SHALL be within 10% of 1024×1024 pixels, the working size of the edit model's reference pipeline.
3. THE Batch_Generator SHALL start the text prompt of every Edit_Template Job with one fixed instruction, defined in the Edit_Template, to keep the person, outfit and location of the reference image. The Slide's scene text SHALL follow the instruction. The instruction SHALL be identical in every Edit_Template Job, whatever the Post and the scene text (invariant).
4. WHERE a carousel Post sets mode `regenerate`, THE Batch_Generator SHALL render every Slide of the Post with the Still_Template and put the Post's location and outfit descriptions in each Slide's text prompt, so that no Slide waits for the Pick of Slide 1.
5. THE Batch_Generator SHALL render as many Candidates for every Slide of a single, carousel or story Post as the Post's Candidate count (a whole number from 1 to 4, default 1). The Candidates SHALL be numbered from 1, and the Candidates of one Slide SHALL have pairwise different seeds (property).
6. IF a Post sets a Candidate count that is not a whole number from 1 to 4, THEN THE Calendar_Reader SHALL reject the Post and name the Post id, the field and the value.
7. WHILE the Prompt_Sheet records no Pick for a Slide of a Post whose Candidate count is 1, THE Batch_Generator SHALL use Candidate 1 as the Slide's Pick, so that a carousel Post with a Candidate count of 1 renders all its Slides in one render stage run.
8. WHILE the Prompt_Sheet records no Pick for a Slide of a Post whose Candidate count is 2 to 4, THE Batch_Generator SHALL hold every Job that uses the Slide's Pick (the Slide's Finish_Template Job and any Edit_Template Job that uses the Pick as its reference image). It SHALL report the Slide as waiting for a Pick, with the number and output file of each rendered Candidate.
9. WHILE the Prompt_Sheet records a Pick for a Slide as a Candidate number from 1 to the Post's Candidate count, THE Batch_Generator SHALL use that Candidate as the Slide's Pick, both for finishing and as the reference image of any Slide that depends on it.
10. IF the Pick that the Prompt_Sheet records for a Slide is not a whole number from 1 to the Post's Candidate count, THEN THE Batch_Generator SHALL report the Post id, the Slide number, the recorded value and the valid Candidate numbers. It SHALL submit no Job that uses the Slide's Pick, run the stage's other Jobs, and exit with a non-zero status.

### Requirement 12: Finishing stills

**User Story:** As the creator, I want every picked still cleaned, face-detailed with my character LoRA and upscaled, so that exports are sharp and the face matches the persona on every slide.

#### Acceptance Criteria

1. WHEN the finish stage runs for a Week_Id, THE Batch_Generator SHALL run one Finish_Template Job, with the Slide's Pick as the input image, for each Slide without a finished image in every single, carousel and story Post of that Week_Id.
2. THE Still_Template, Edit_Template, Finish_Template and Refine_Template SHALL connect the image output of every VAE decode node that decodes the whole image (not a face crop), including the Wan 2.1 upscale2x sharp decode, only to a ComfyUI-DeGrid `VAEDeGrid` node, so that every resize, face-detailing, sharpening, upscaling and save node receives the image after grid removal.
3. THE Finish_Template SHALL detail every detected face with Krea 2 Raw bf16, the official Krea 2 turbo LoRA at strength 0.6 and the stills Character_LoRA at the Persona_File's strength (default 1.0), using a prompt that contains the Trigger_Word and contains the Identity_Paragraph verbatim exactly once.
4. THE Finish_Template SHALL upscale each Pick, whether the Still_Template or the Edit_Template rendered it, keeping its width-to-height ratio within 1%, to at least twice the width and twice the height of the Post's export size: at least 2,160×2,880 pixels for a 1080×1440 Feed_Size, 2,160×2,700 pixels for a 1080×1350 Feed_Size, and 2,160×3,840 pixels for the Vertical_Size of a story Post.
5. THE Batch_Generator and the Export_Tool SHALL use as a Slide's finished image only the output of a finish Job that the Manifest records as done and whose Job_Key equals the one computed from the Slide's current Pick and Persona_File, so that an image finished from an earlier Pick is never exported or used as a Reel's first frame.
6. IF the Finish_Template detects no face in a Pick, THEN THE Finish_Template SHALL upscale the Pick as in criterion 4 without face detailing and without an execution error.

### Requirement 13: Instagram-ready export

**User Story:** As the creator, I want every post exported at Instagram's native sizes and formats, so that uploads keep their framing and quality.

#### Acceptance Criteria

1. THE Export_Tool SHALL export every Slide of a single or carousel Post at the Post's Feed_Size: 1080×1350 pixels for a Post that sets the aspect ratio 4:5, and 1080×1440 pixels for every other single or carousel Post.
2. THE Export_Tool SHALL export story Slides and Reels at the Vertical_Size.
3. THE Export_Tool SHALL crop each source image or video frame to the largest centred box with the target aspect ratio, then resize the crop with Lanczos resampling to exactly the target width and height.
4. THE Export_Tool SHALL compute a crop box that meets all of these conditions for every source width and height from 1 to 16,384 pixels and each target size (1080×1440, 1080×1350 and 1080×1920) (property):
   - it lies inside the source bounds and is at least 1 pixel wide and 1 pixel high
   - it spans the full source width or the full source height
   - its width is within 1 pixel of its height × target width ÷ target height, or its height is within 1 pixel of its width × target height ÷ target width
   - its left and right margins differ by at most 1 pixel, and so do its top and bottom margins
5. IF the source of a Slide is narrower than the target width or shorter than the target height, THEN THE Export_Tool SHALL export the Slide and record the flag "upscaled at export" for that Slide.
6. THE Export_Tool SHALL export all Slides of one carousel at the same width and height.
7. THE Export_Tool SHALL write images as sRGB JPEG at quality 95 or higher, with an embedded sRGB profile.
8. THE Export_Tool SHALL copy none of a source file's metadata into its exported file, so that no exported image or Reel contains ComfyUI's embedded prompt or workflow.
9. THE Export_Tool SHALL embed in every exported image the IPTC Digital Source Type property, set to the IPTC controlled-vocabulary URI for `trainedAlgorithmicMedia`.
10. THE Export_Tool SHALL encode every exported Reel as an MP4 file with one H.264 video track in the yuv420p pixel format, with the source's frame rate and the source's frame count.
11. WHEN the user gives the Export_Tool a video made outside the Batch_Generator (for example a wan-animate render) for a reel Post, THE Export_Tool SHALL export that video as the Post's Reel: as the only Reel source of a Post that sets mode `external` (Requirement 16, criterion 15), and in place of any Reel the Batch_Generator rendered for any other reel Post. It SHALL use the Vertical_Size and the same crop, encoding and file naming as a Batch_Generator Reel.
12. THE Export_Tool SHALL write each exported file inside the Post's output folder from Requirement 10, criterion 11. Images SHALL be named `<post-id>_<NN>.jpg` and Reels `<post-id>_<NN>.mp4`. NN SHALL be the two-digit Slide number in upload order (01 to 20), and 01 for a Reel.
13. WHEN the Export_Tool exports a Reel whose source has an audio track, THE Export_Tool SHALL encode that audio as one AAC track in the exported MP4.
14. IF a Slide of a single, carousel or story Post has no finished image, a reel Post that sets mode `external` has no user-given video, or any other reel Post has neither a rendered Reel nor a user-given video, THEN THE Export_Tool SHALL leave the Post's exported files as they were before the run, report the Post as waiting with the missing Slide numbers or the missing Reel, and continue with the next Post.
15. IF the Export_Tool cannot read a source or cannot write an exported file of a Post, THEN THE Export_Tool SHALL leave the Post's exported files as they were before the run, report the Post as failed with the file path and the error, and continue with the next Post.

### Requirement 14: One colour grade on every post

**User Story:** As the creator, I want the same LUT and grain on every still and Reel, so that the feed reads as one photographer's work.

#### Acceptance Criteria

1. WHEN the Export_Tool exports an image or a Reel (including a video made outside the Batch_Generator), THE Export_Tool SHALL apply the Persona_File's 3D LUT to every pixel of the image or of every Reel frame, after the crop and resize of Requirement 13 and before encoding. It SHALL use v/255 as the LUT input for each 8-bit sRGB channel value v, clamp LUT outputs to the range 0.0 to 1.0, and leave the source file unchanged.
2. WHEN the Export_Tool has applied the LUT to an image or a Reel frame, THE Export_Tool SHALL add film grain to it before encoding, with results clamped to the range 0 to 255, at the Persona_File's grain strength. The grain strength is a number from 0.0 to 1.0. At 0.0 no grain is added. For a uniform mid-grey source (128 in every channel) with an identity LUT, a higher strength SHALL give a larger standard deviation of the graded pixel values.
3. THE Export_Tool SHALL derive the grain pattern of each image from the Week_Id, Post id and Slide index, and the grain pattern of each Reel frame from the Week_Id, Post id and frame index, so that any two Slides of a Post, and any two frames of a Reel, get different patterns.
4. WHEN the Export_Tool exports a Post again on the same machine and Python environment, with unchanged source files, Post, LUT file and grain strength, THE Export_Tool SHALL write image files that are byte-identical to those of the previous export (idempotence).
5. WHEN the Export_Tool applies an identity LUT of any N from 2 to 65 with grain strength 0.0, THE Export_Tool SHALL produce pixel values, before encoding, that differ from the cropped and resized source by at most 1 in every channel of every pixel (metamorphic property). An identity LUT has the default domain, and its entry at each red, green and blue lattice index (i, j, k) is (i, j, k)/(N−1) to within 0.000001.
6. IF the LUT_Reader rejects the LUT that the Persona_File names, or the Persona_File's grain strength is missing or is not a number from 0.0 to 1.0, THEN THE Export_Tool SHALL write nothing to the Output_Root, report the file path and the reason, and exit with a non-zero status.
7. THE LUT_Reader SHALL read 3D `.cube` files that have one `LUT_3D_SIZE` line giving N from 2 to 65, optional `TITLE`, `DOMAIN_MIN` and `DOMAIN_MAX` lines (the default domain is 0.0 to 1.0 in each channel), and then N³ RGB entry lines in `.cube` order, with the red index changing fastest. Blank lines and lines starting with `#` are allowed anywhere.
8. IF a `.cube` file has any of these faults, THEN THE LUT_Reader SHALL reject the file and report the file path and the fault. It SHALL also report the 1-based number of the first faulty line when the fault is on a line, and the expected and actual counts for a wrong entry count:
   - a `LUT_1D_SIZE` line
   - no `LUT_3D_SIZE` line, an N outside 2 to 65, or a repeated keyword line
   - a `DOMAIN_MIN` value that is not below the matching `DOMAIN_MAX` value
   - an entry line that is not exactly 3 finite numbers
   - a number of entries other than N³
9. THE LUT_Printer SHALL write every valid LUT so that the LUT_Reader reads back an equal LUT, with the same N, TITLE, domain and entry values (round-trip property).
10. THE Repo SHALL include two `.cube` files made for the Repo. Each SHALL be accepted by the LUT_Reader and have a `#` comment line that states its origin and licence:
    - an identity LUT, as defined in criterion 5
    - an example grade LUT with at least one entry that differs from the identity entry at the same lattice index by more than 1/255

### Requirement 15: QC sheets and publish sheets

**User Story:** As the creator, I want a side-by-side QC view against the hero image and a publish sheet per post, so that I catch identity and continuity errors and post by hand with the right labels.

#### Acceptance Criteria

1. WHEN the export stage completes for a Week_Id, THE Export_Tool SHALL write a QC_Sheet for every single, carousel or story Post with status exported. Each QC_Sheet SHALL have one row per Slide in ascending Slide number. Each row SHALL show the Hero_Image first, then every rendered Candidate of the Slide in ascending Candidate number, with each image scaled to a height of 1,024 pixels and its aspect ratio kept.
2. WHEN the export stage completes for a Week_Id, THE Export_Tool SHALL write a QC_Sheet for every reel Post with status exported. The QC_Sheet SHALL have one row showing the Hero_Image, then the first frame, frame ⌊F/2⌋ + 1 and the last frame of the exported Reel, where F is the Reel's frame count. Each image SHALL be scaled to a height of 1,024 pixels with its aspect ratio kept.
3. THE Export_Tool SHALL print on every QC_Sheet the Persona_File's signature items and these checks: face, hands, teeth, jewellery continuity, and legible background text.
4. WHEN the export stage completes for a Week_Id, THE Export_Tool SHALL write one week index in the Week_Id's folder under the Output_Root. The week index SHALL list every Post of the Content_Calendar once, with:
   - its status
   - the paths of its QC_Sheet and Publish_Sheet, where they exist
   - each flag that applies: review (the Prompt_Sheet flags the Post for review), failed Jobs (the Manifest records a failed Job for the Post) and upscaled at export (a Slide of the Post is flagged upscaled at export)
5. WHEN the export stage completes for a Week_Id, THE Export_Tool SHALL write a Publish_Sheet for every Post with status exported. Each Publish_Sheet SHALL list:
   - the exported files in upload order (ascending Slide number), each with its Slide's alt text
   - the caption, disclosure line and hashtags, character for character as the Prompt_Sheet records them
   - the Post's planned date from the Content_Calendar
6. WHEN the export stage completes for a Week_Id, THE Export_Tool SHALL print the number of exported, failed and waiting Posts, the absolute Output_Root path and a reminder to stop the Instance. The three counts SHALL add up to the number of Posts in the Content_Calendar.
7. THE Export_Tool SHALL label each Candidate on a QC_Sheet with:
   - the Post id
   - the Slide number used in the exported file name
   - the Candidate number, counting from 1
   - the seed that the Manifest records for the Candidate
   - the name of the Render_Template that rendered it
   - on the Pick of each Slide, a mark that identifies it as the Pick
8. THE Export_Tool SHALL give each Post exactly one of these statuses:
   - exported: every Slide of the Post, or its Reel, has an exported file
   - failed: the Post is not exported, and either the Manifest records a failed Job for it or one of its files failed to export
   - waiting: the Post is neither exported nor failed
9. THE Export_Tool SHALL include this disclosure checklist, as unticked items, in the Publish_Sheet of every Instagram Post and in no Publish_Sheet of a Post with platform `other`:
   - "AI-generated profile" label on
   - AI info label on the post
   - AI disclosure in the bio
   - no health claims
   - no DMs implying a real person
   - Instagram's nudity rules followed

### Requirement 16: Reels with MiniMax H3 (`influencer-video`)

**User Story:** As the creator, I want short Reels made from a finished still or from my reference pack, so that video posts show the same person as the stills.

#### Acceptance Criteria

1. WHEN `install-workflow.sh influencer-video` completes without errors, THE Provisioner SHALL have installed:
   - every model file of the `minimax-h3` lines (the H3 weights, the H3 turbo 8-step LoRA and taeh3), each at the path its `minimax-h3` line installs to
   - the H3 look LoRAs listed for `influencer-video` in Appendix A
   - every node pack named in the node-class-to-pack maps of the Reel_Template and Ref_Reel_Template
   - the `ffmpeg` binary on the PATH
2. WHEN the reels stage runs for a Week_Id, THE Batch_Generator SHALL render each reel Post in the Week_Id's Content_Calendar that names a source Slide with the Reel_Template. The Reel SHALL use the source Slide's finished image as the first frame and a prompt that contains the Post's motion description from the Prompt_Sheet verbatim. A reel Post that names a Post without a Slide number names that Post's Slide 1.
3. WHEN the Batch_Generator builds a Reel_Template Job, THE Batch_Generator SHALL centre-crop the source Slide's finished image to the Reel's width-to-height ratio, then scale the crop to the Reel's exact width and height. The crop box SHALL lie inside the source bounds and be centred to within 1 pixel (property).
4. WHEN the reels stage runs for a Week_Id, THE Batch_Generator SHALL render each reel Post in the Week_Id's Content_Calendar that sets mode `reference` with the Ref_Reel_Template. The Reel SHALL use every Reference_Pack image (1 to 5), in the Persona_File's order, and a prompt that contains the Post's motion description from the Prompt_Sheet verbatim.
5. THE Reel_Template and Ref_Reel_Template SHALL sample with 8 steps and load the H3 turbo 8-step LoRA at strength 1.0, followed by the Persona_File's video look LoRAs in the listed order and at the listed strengths.
6. WHERE the Persona_File names a video Character_LoRA, THE Reel_Template and Ref_Reel_Template SHALL load that LoRA at the Persona_File's strength (default 1.0), after the H3 turbo LoRA and before the first video look LoRA.
7. THE Batch_Generator SHALL set each Reel's frame count to the 17n+5 value (n ≥ 1) nearest to 24 × the reel Post's duration in seconds, taking the larger value on a tie. The frame count SHALL be capped at the largest 17n+5 value that does not exceed the maximum frame count in the Render_Template metadata. Before the cap, 5 s gives 124 frames, 8 s gives 192 and 10 s gives 243. Property: every result has the form 17n+5 and does not exceed the metadata maximum, and every result that the cap does not lower is within 8.5 frames (17/48 s) of the requested duration.
8. THE Batch_Generator SHALL choose each Reel's width and height as multiples of 32 with a pixel area of at most 768×1344. It SHALL take the pair with the largest area whose width-to-height ratio is within 1% of 9:16 and, among pairs with equal area, the pair whose ratio is closest to 9:16 (property). For the 9:16 target this gives 736×1312 (ratio 0.5610, 0.27% from 9:16). The Export_Tool's crop and resize of Requirement 13, criterion 3 then produce the exact Vertical_Size.
9. WHERE a reel Post that does not set mode `external` provides dialogue lines and a voice clip, THE Batch_Generator SHALL pass the voice clip as the audio input of the Reel's Render_Template and place each line, unchanged and in the listed order, inside its own `<d>…</d>` pair in the prompt.
10. IF a reel Post does either of the following, THEN THE Calendar_Reader SHALL reject the Post and name the Post id and the reason:
    - sets more than one of a source Slide, mode `reference` and mode `external`, or none of the three
    - sets mode `reference` while the Reference_Pack lists no images
11. IF an Instagram reel Post names a source Slide from a Post with platform `other`, THEN THE Calendar_Reader SHALL reject the reel Post and name both Post ids.
12. IF a reel Post that does not set mode `external` provides dialogue lines without a voice clip, a voice clip without dialogue lines, or a voice clip that does not exist (resolved relative to the Influencer_Root), THEN THE Calendar_Reader SHALL reject the Post and name the Post id and the missing item.
13. WHERE a reel Post sets mode `reference` and the Persona_File lists Location_Shots for the Post's location, THE Batch_Generator SHALL pass the first 4 of those Location_Shots in the Persona_File's order (all of them when it lists fewer) to the Ref_Reel_Template as extra reference images after the Reference_Pack images, so that a Job has at most 9 reference images.
14. IF a reel Post sets mode `reference` and a Location_Shots file that the Persona_File lists for the Post's location does not exist (resolved relative to the Influencer_Root), THEN THE Calendar_Reader SHALL reject the Post and name the Post id and the missing file.
15. WHERE a reel Post sets mode `external`, THE Batch_Generator SHALL submit no Job for the Post in the reels stage and report the Post as waiting for an external video, so that the Post stays waiting (Requirement 13, criterion 14) until the user gives the Export_Tool a video for the Post (Requirement 13, criterion 11).

### Requirement 17: Training kit install

**User Story:** As the creator, I want one idempotent command that installs ostris/ai-toolkit on the instance volume in its own environment, so that I can train character LoRAs without touching ComfyUI's Python.

#### Acceptance Criteria

1. WHEN the Training_Kit install runs, THE Training_Kit SHALL check out the ai-toolkit commit pinned in the Repo (a full 40-character commit SHA) into the install folder (`/workspace/ai-toolkit`, or another folder under `/workspace` that an environment variable names), and install that commit's Python dependencies into the Training_Env with the torch version and CUDA build pinned in the Repo, leaving the ComfyUI_Env's installed package list unchanged.
2. WHILE the install folder is at the pinned commit, the Training_Env's Python interpreter runs, and the last install into the Training_Env finished every step for that commit and the pinned torch build, WHEN the Training_Kit install runs, THE Training_Kit SHALL download nothing, leave the install folder's tracked files and the Training_Env's installed packages unchanged, and print that ai-toolkit is already at the pinned commit.
3. IF the Training_Env exists but its Python interpreter is missing or exits with a non-zero status when asked for its version (for example on a new image with a reused volume), THEN THE Training_Kit SHALL delete and recreate the Training_Env, reinstall its packages, delete no other file, and print that the Training_Env was rebuilt and why.
4. WHEN a Training_Kit install run ends with no failed install step, including a run that changed nothing, THE Training_Kit SHALL print the Training_Env's torch version, the CUDA version torch was built for, and the name and total memory of each GPU that torch sees, or state that torch sees no GPU.
5. WHILE a Training_Kit install runs on the Instance, WHEN a second Training_Kit install starts, THE Training_Kit SHALL make the second install print that another install is running and wait until the first install exits, changing no file in the install folder or the Training_Env while it waits.
6. THE Training_Kit SHALL store the Hugging Face downloads of every training run and ai-toolkit UI it starts, including the jobs the UI starts, in one cache folder on the `/workspace` volume and in no folder outside that volume.
7. WHEN the user starts the ai-toolkit UI through the Training_Kit, THE Training_Kit SHALL make the UI listen on 127.0.0.1, port 8675 (ai-toolkit's default), and on no other address, such as 0.0.0.0.
8. THE Training_Kit SHALL hand HF_TOKEN to ai-toolkit only through the environment of the training runs and the ai-toolkit UI that it starts, writing the token value to no file, including ai-toolkit's `.env` file and settings database.
9. IF an install step fails (cloning, fetching or checking out ai-toolkit, creating the Training_Env, installing a package, or importing torch in the Training_Env), or torch in the Training_Env sees no GPU, THEN THE Training_Kit SHALL stop at that failure, name the failed step or the missing GPU, and exit with a non-zero status.
10. IF the install folder is in one of these states, THEN THE Training_Kit SHALL stop before changing any file, print the folder and the reason, and exit with a non-zero status:
    - outside `/workspace`
    - not empty and not a git checkout of the ostris/ai-toolkit GitHub repository
    - at a commit other than the pinned commit, with uncommitted changes to tracked files
11. WHEN the ai-toolkit UI starts listening, THE Training_Kit SHALL print an SSH tunnel command of the form `ssh -p <port> root@<host> -L 8675:127.0.0.1:8675` and the address to open through the tunnel, `http://127.0.0.1:8675`.

### Requirement 18: Datasets

**User Story:** As the creator, I want my curated character images checked and captioned consistently, so that the character LoRA trains on clean, correctly tagged data.

#### Acceptance Criteria

1. THE Training_Kit SHALL read a Dataset from the entries directly inside `Influencer_Root/dataset/<version>/` whose names do not start with a dot, treating each `.jpg`, `.jpeg`, `.png` or `.webp` file (any letter case) as one image and the `.txt` file with the same base name (for example `img01.txt` for `img01.jpg`) as that image's caption file.
2. WHEN the Training_Kit checks a Dataset, THE Training_Kit SHALL report the image count and list, by file name:
   - as errors: each image that cannot be decoded, each unsupported entry (a subfolder, or a file that is neither an image nor an image's caption file), and each group of byte-identical images
   - as warnings: each image whose shorter side is below 1024 pixels, with its width and height, and each image without a caption (no caption file, or a caption file that holds only whitespace)
3. IF a checked Dataset holds fewer than 25 or more than 35 images, THEN THE Training_Kit SHALL print a warning that states the image count and the recommended range of 25 to 35 images.
4. WHEN the Training_Kit finishes preparing captions for a Dataset, THE Training_Kit SHALL have made the Trigger_Word the first word of every caption and removed every other occurrence of the Trigger_Word from it (whole word, any letter case), so that preparing the captions again leaves every existing caption file byte-identical (idempotence property).
5. WHERE vision captioning is enabled, WHEN the Training_Kit prepares captions, THE Training_Kit SHALL send each image that has no caption to the Ollama vision model named by the vision-captioning option, through the Ollama_API, with a request for a one-sentence description of the clothing, pose, setting and lighting, and write the description to that image's caption file.
6. IF a caption file of a Dataset that the Training_Kit checks or prepares contains text that the Safety_Guard rejects, THEN THE Training_Kit SHALL reject the Dataset with a non-zero exit status before writing any caption file, and name each such file and the matched term.
7. WHEN the refine stage runs for a Dataset, THE Batch_Generator SHALL render every image of the Dataset through the Refine_Template at the configured denoise (default 0.30, allowed range 0.10 to 0.50) and save each result under the source image's base name, with a copy of its caption file, in the new Dataset version `<version>-refined`, leaving every file of the source Dataset unchanged (invariant).
8. IF the folder of a checked Dataset is missing or holds no image, or the check lists an error, THEN THE Training_Kit SHALL end the check with a non-zero exit status.
9. WHERE vision captioning is enabled, IF the Ollama_API does not answer within 10 seconds or does not list the named vision model, THEN THE Training_Kit SHALL stop with a non-zero exit status before writing any caption file, and report the URL it tried or the missing model name.
10. WHERE vision captioning is enabled, IF the Ollama_API returns no description within 120 seconds, or returns one that is empty, spans more than one line, is longer than 300 characters or contains text that the Safety_Guard rejects, THEN THE Training_Kit SHALL discard the description and retry up to 3 times. After the last failure THE Training_Kit SHALL leave the image without a caption, list the file, continue with the remaining images, and end with a non-zero exit status.
11. WHEN the refine stage runs for a Dataset, THE Batch_Generator SHALL record every refine Job in a Manifest in the folder of the new Dataset version, under a file name that starts with a dot, so that criterion 1 leaves the Manifest out of the Dataset. THE Batch_Generator SHALL apply Requirement 10, criteria 3 (skipping done Jobs), 9 (atomic Manifest writes), 12 (Manifest round trip) and 13 (resuming submitted Jobs), to refine Jobs and this Manifest.
12. WHILE a Batch_Generator process runs the refine stage for a Persona, THE Batch_Generator SHALL refuse to start a second refine stage process for the same Persona: the second process SHALL report the running process id and exit with a non-zero status before it submits any Job or writes a Manifest. A process that has exited or been killed SHALL not count as running.

### Requirement 19: Training runs and checkpoints

**User Story:** As the creator, I want a run-ready training config generated from my persona and a detached training run, so that a multi-hour run survives a closed browser tab and its checkpoints appear in ComfyUI.

#### Acceptance Criteria

1. WHEN the user requests a training config with a run name and a Dataset version, THE Training_Kit SHALL write one ai-toolkit config to `Influencer_Root/training/<run>/`. The config SHALL be the committed Krea 2 Raw example with four values filled in: the Persona_File's Trigger_Word, the Dataset folder `Influencer_Root/dataset/<version>/`, an output folder inside `Influencer_Root/training/<run>/`, and the run name. Every other setting SHALL stay as in the example.
2. THE Repo SHALL include an example ai-toolkit config for a MiniMax H3 character LoRA. Its file header and a Runbook note SHALL each state that the config is experimental and unverified on one 80 GB GPU.
3. WHEN the Training_Kit prepares a training base model (such as `krea/Krea-2-Raw`), THE Disk_Estimator SHALL report two sizes before any base-model download starts, both in GB of 10^9 bytes rounded to 1 decimal:
   - the size still to download: the files in the model's Hugging Face file listing that are not yet in the Hugging Face cache
   - the free space on `/workspace`

   IF the Disk_Estimator cannot determine the size still to download, THEN it SHALL report that size as unknown.
4. IF the free space on `/workspace` is below the size still to download plus 20 GB (compared before rounding), or that size is unknown, THEN THE Training_Kit SHALL stop before any base-model download or training starts, exit with a non-zero status, and report the free space and the required space.
5. IF Hugging Face refuses a base-model download from a gated repository (such as `krea/Krea-2-Raw`), THEN THE Training_Kit SHALL stop before training starts and exit with a non-zero status. It SHALL name the repository, state whether HF_TOKEN is set, and state that the Hugging Face account must accept the licence or be approved.
6. WHEN a training run starts, THE Training_Kit SHALL ask the ComfyUI_API and the Ollama_API to unload every loaded model before it launches the training process. It SHALL wait at most 10 seconds for each API before it continues.
7. WHEN a training run starts, THE Training_Kit SHALL launch ai-toolkit in the Training_Env as a background process. The process SHALL keep running after the terminal, SSH session or browser tab that started it closes.
8. WHILE a training run is active on the Instance (from the moment the Training_Kit accepts its start until its training process exits), IF another training run is started, including one started at the same moment in another terminal, THEN THE Training_Kit SHALL refuse to start it and exit with a non-zero status. It SHALL report the active run's name, process id and log file.
9. WHEN the user runs the publish step for a training run that is not active, THE Training_Kit SHALL copy the run's checkpoint `.safetensors` files from its output folder, and no other file, under their own names to `models/loras/characters/<slug>/`. ComfyUI then lists each file as `characters/<slug>/<file>`, and publishing twice gives the same result as publishing once (idempotence).
10. IF a training config request fails one of these checks, THEN THE Training_Kit SHALL write no file, exit with a non-zero status, and name the value that failed:
    - the run name has 1 to 32 characters, all lowercase letters, digits or hyphens
    - the Dataset version exists under `Influencer_Root/dataset/` and holds at least one image
    - `Influencer_Root/training/<run>/` does not already hold a config
11. WHEN a training run starts, THE Training_Kit SHALL append the training process's standard output and standard error to one log file in `Influencer_Root/training/<run>/`. When the process exits, the log SHALL end with its exit status.
12. IF the publish step runs for a training run that is still active, THEN THE Training_Kit SHALL copy no file, exit with a non-zero status, and report that the run is still active.

### Requirement 20: LoRA test grid

**User Story:** As the creator, I want every saved checkpoint rendered on the same varied prompts next to the hero image, so that I can pick the best checkpoint and see which angles need retraining.

#### Acceptance Criteria

1. THE Repo SHALL include an example Test_Grid of 20 prompts, in which each prompt names its angle, expression, outfit, lighting and location and has a fixed seed from 0 to 4,294,967,295, and the 20 prompts together use at least 5 angles (including front, three-quarter and profile), 4 expressions, 3 lighting setups, 2 of the Persona_Example's outfits and 3 of its locations.
2. WHEN the grid stage runs for a list of checkpoints and a list of strengths, THE Batch_Generator SHALL run one grid Job with the Still_Template for every combination of Test_Grid prompt, checkpoint and strength, using the prompt's Test_Grid seed and loading the checkpoint at that strength in place of the stills Character_LoRA. The default checkpoint list is every `.safetensors` file in `models/loras/characters/<slug>/`; the default strength list is the Persona_File's stills Character_LoRA strength.
3. WHEN every grid Job of one checkpoint and strength is done or failed, THE Batch_Generator SHALL write one QC_Sheet for that checkpoint and strength, showing the checkpoint file name and strength, the Hero_Image, and the 20 renders in prompt order, each labelled with its prompt number and angle, with a placeholder marked failed in place of each failed render.
4. WHEN the grid stage runs again, THE Batch_Generator SHALL skip every grid Job whose output file exists and whose Job_Key, computed from the filled prompt and the checkpoint file's content, the grid stage's Manifest records as done, so that a checkpoint replaced under the same file name renders again.
5. THE Batch_Generator SHALL build each grid Job's text prompt, look LoRAs and image size as for a `single` Post at 3:4 whose scene text, location and outfit are those of the Test_Grid prompt, so that the grid Jobs of one prompt differ only in the Character_LoRA file, its strength and the output file names (invariant).
6. IF the checkpoint list or the strength list is empty, the checkpoint list holds more than 20 checkpoints, the strength list holds more than 5 strengths, or a strength is outside 0.1 to 2.0, THEN THE Batch_Generator SHALL stop before submitting any Job and report the list, the offending value or count, and the limit.
7. IF a Test_Grid holds other than 20 prompts, or a prompt's seed is missing or outside 0 to 4,294,967,295, a prompt names an outfit or location that the Persona_File does not define, or a prompt contains text that the Safety_Guard rejects, THEN THE Batch_Generator SHALL reject the Test_Grid before submitting any Job and report every error with its reason and, for an error in a prompt, the prompt number.
8. WHILE a Batch_Generator process runs the grid stage for a Persona, THE Batch_Generator SHALL refuse to start a second grid stage process for the same Persona: the second process SHALL report the running process id and exit with a non-zero status before it submits any Job or writes the grid stage's Manifest. A process that has exited or been killed SHALL not count as running.

### Requirement 21: NSFW add-on and separation from Instagram

**User Story:** As the creator, I want adult-content adapters installed only on request and kept apart from Instagram posts, so that nothing explicit reaches the Instagram account by accident.

#### Acceptance Criteria

1. WHEN `install-workflow.sh influencer-nsfw` exits with status 0, THE Provisioner SHALL have pulled the Ollama model `dolphin3:8b` and installed every LoRA listed for `influencer-nsfw` in Appendix A under `models/loras/nsfw/`.
2. THE Config_Lists SHALL tag every NSFW_Set line with `influencer-nsfw` and no other id.
3. IF an Instagram Post (any Post whose platform is not `other`, including a Post with no platform) lists a look LoRA outside the Instagram_Allowlist at any strength, or names a source Slide from a Post with platform `other`, THEN THE Batch_Generator SHALL, in the plan, render, finish and reels stages, submit no Job for the Post, name the Post id and the LoRA or Slide, continue with the other Posts, and exit with a non-zero status when the stage ends.
4. WHEN the plan stage requests text for a Post from the Ollama_API, THE Batch_Generator SHALL use the Persona_File's Ollama model for platform `other` (default `dolphin3:8b`) only if the Post's platform is `other`, and the Persona_File's model for `instagram` (default `qwen3:8b`) for every Instagram Post.
5. THE Runbook SHALL state:
   - a summary of what Instagram's Community Guidelines allow and forbid in nudity and sexual content, with a link to the guidelines and the date the summary was checked
   - that adult content goes only into Posts with platform `other`, for publishing only on platforms whose terms allow adult content from AI-generated creators that are labelled as AI
6. IF a Character_LoRA of the Persona_File, or a LoRA in a look-LoRA stack that the Persona_File applies to Instagram Posts (stills or video), is outside the Instagram_Allowlist, THEN THE Batch_Generator SHALL reject the Persona_File, name the field and the LoRA, and exit with a non-zero status before requesting text from the Ollama_API or submitting any Job.
7. THE Batch_Generator SHALL count a LoRA as inside the Instagram_Allowlist only when its name, read as a path relative to `models/loras/`, exactly matches (letter case included) one of these:
   - the path, relative to `models/loras/`, of a LoRA that an active models.list line tagged `influencer` or `influencer-video` installs
   - a Character_LoRA path of the Persona_File that is relative, has no `..` segment and is not under `models/loras/nsfw/`
8. THE Batch_Generator and Export_Tool SHALL write every render, export, QC_Sheet and Publish_Sheet of a Post with platform `other` inside `Output_Root/<Week_Id>/other/`, and every such file of an Instagram Post outside that folder, for every Content_Calendar that the Calendar_Reader accepts (path-containment property).

### Requirement 22: Character design (`character-design`)

**User Story:** As the creator, I want local tools that turn a sketch into a hero image and make multi-view character sheets, so that I can fill missing dataset angles without going back to closed models.

#### Acceptance Criteria

1. WHEN `install-workflow.sh character-design` exits with status 0, THE Provisioner SHALL have installed:
   - `krea2_lineart_edit_v1`, `krea2_lineart_coarse_edit_v1` and the workflow `lineart_example.json`, from HF `Cierpliwy/krea2_lineart_edit`
   - `QuadView_krea2_v1`, `DynamicCharacterSheet_krea2_v1` and every `workflows/*.json` file that loads one of them, from HF `Alissonerdx/CharacterSheet`
   - the node packs `ostris/ComfyUI-Krea2-Ostris-Edit` and `comfyui_controlnet_aux`
   - every other model file that a loader in these workflows names, under the loader's file name or the name that a patch in `config/patches/` sets for that loader
   - the node pack of every node class in these workflows that ComfyUI core does not provide
2. THE Config_Lists SHALL have no active line tagged `character-design` that installs a Flux.2 Klein LoRA variant of HF `Alissonerdx/CharacterSheet`, or a workflow of that repository that loads a Flux.2 Klein model, because the Flux.2 Klein base model needs approval from Black Forest Labs.
3. THE Runbook SHALL describe, as numbered steps, how to add `character-design` outputs to a Dataset to cover missing angles:
   - find the missing angles in the Test_Grid QC_Sheets, or by comparing the Dataset with the Runbook's Dataset shot list
   - render a character sheet from the Hero_Image or a Dataset image with a QuadView or DynamicCharacterSheet workflow
   - crop each view into its own image file whose shorter side is at least 1024 pixels
   - copy the crops into a new Dataset version and leave the source Dataset unchanged
   - prepare captions and run the Dataset check with the Training_Kit, and confirm that the new version holds 25–35 images
4. THE Config_Lists SHALL add `character-design` to the tag of every line that installs a file or node pack that the `character-design` UI workflows load, instead of adding a second line for the same file or node pack. An untagged line, such as `comfyui_controlnet_aux`, SHALL become `wf:global,character-design`, so that `global` and full runs still select it.
5. THE Runbook SHALL describe, as numbered steps, how to turn a sketch into a Hero_Image:
   - load the sketch into `lineart_example.json`
   - choose `krea2_lineart_edit_v1` or `krea2_lineart_coarse_edit_v1`, and state which kind of sketch each one suits
   - set the chosen render as the Persona_File's Hero_Image

### Requirement 23: Disk estimates

**User Story:** As the creator, I want to know how much disk each id and each training base model needs before I rent or download, so that I size the instance correctly.

#### Acceptance Criteria

1. WHEN the Disk_Estimator runs for one or more Catalog ids, THE Disk_Estimator SHALL report four figures in GB (1 GB = 10^9 bytes), rounded to 1 decimal place, over the items in the combined Selection of those ids. An item is a distinct models.list install path or Ollama model, so an item shared by several ids or lines counts once. The figures are:
   - size already present: the installed size of the items that the Provisioner would skip as already installed
   - size still to download: the size reported by the source of every other item
   - total download size: the sum of the two, computed before rounding
   - free space: the free space on `/workspace`
2. IF the Disk_Estimator cannot determine the size of an item or a training base model without downloading it, because the source refuses the request, reports no size or does not answer within 10 seconds, THEN THE Disk_Estimator SHALL list it as unknown by filename or repository, leave it out of every size figure and report the number of unknowns.
3. WHEN `install-workflow.sh` starts for an id, THE Provisioner SHALL print the Disk_Estimator's size still to download and number of unknowns for the id before the node, model and Ollama phases start.
4. IF the Disk_Estimator exits with an error, or cannot reach a source within 10 seconds, during `install-workflow.sh`, THEN THE Provisioner SHALL continue the install, with the same steps and exit status as a run without the estimate.
5. IF `/workspace` does not exist (for example on the Mac), THEN THE Disk_Estimator SHALL count every item as still to download and report the free space as not measured.
6. IF the size still to download that `install-workflow.sh` prints exceeds the free space on `/workspace`, THEN THE Provisioner SHALL print a warning that states both sizes, without stopping the install.
7. WHEN the Disk_Estimator runs for a training base model (`krea/Krea-2-Raw`, or the experimental `MiniMaxAI/MiniMax-H3`), THE Disk_Estimator SHALL report the total size of the files that the Training_Kit downloads for that model, taken from the model's Hugging Face file listing, and the free space on `/workspace`, in GB (1 GB = 10^9 bytes) rounded to 1 decimal place.
8. IF a requested id is not in the Catalog, or a requested base model is not one of the base models in criterion 7, THEN THE Disk_Estimator SHALL exit with a non-zero status without reporting any size, naming the value and listing the valid ids and base models.

### Requirement 24: Online source check

**User Story:** As the maintainer, I want an online check that every source still resolves and is still free, so that a moved file or a new paywall is caught before an instance boots.

#### Acceptance Criteria

1. WHEN the Source_Checker runs for a list of ids (default: every Catalog id), THE Source_Checker SHALL check every active (uncommented) line in the Selections of those ids, downloading at most 1 MB of the content that each line installs. It SHALL report each line once, with its Config_Lists file, line number and source, and whether the source resolves (with the host's answer when it does not). A source resolves when:
   - Hugging Face: Hugging Face accepts a download of the file at the line's `::path` (the filename when the line has no path), made with HF_TOKEN when it is set
   - Civitai: Civitai accepts a download of the version id (the part before any `::`), made with CIVITAI_TOKEN
   - URL: the server answers HTTP 200 after following redirects
   - git: the repository exists at the URL and, when the line names a ref, that branch or tag exists
   - Ollama: the Ollama library has the model at the line's tag (`latest` when the line has none)

   A `bundled` line is reported as local and is not checked online.
2. IF Civitai reports a version as paid or in early access, THEN THE Source_Checker SHALL report the line as not free in place of any other result, with the early-access end date (ISO 8601, UTC) when Civitai returns one.
3. WHEN a Hugging Face or Civitai line resolves, THE Source_Checker SHALL report one of these: the licence that the Hugging Face repository declares (its `license` value, or its `license_name` when that value is `other`), the commercial-use permissions that Civitai lists for the model, or that the source declares none.
   - For a line whose `wf:` tag lists an Influencer_Id, THE Source_Checker SHALL also report the Licence_Gate result for that licence or those permissions: failed, or passed. A pass SHALL come with the conditions that the line's `# licence:` line records.
   - For every other line, the licence SHALL be information only, without effect on the exit status.
4. IF any checked line is reported as not resolving, not free, failing the Licence_Gate, access denied or unreachable, THEN THE Source_Checker SHALL finish checking and reporting every selected line and then exit with a non-zero status.
5. IF an id given to the Source_Checker is not listed in the Catalog, THEN THE Source_Checker SHALL name the id, list the Catalog ids and exit with a non-zero status before it contacts any source.
6. IF CIVITAI_TOKEN is unset or rejected for a Civitai line, or Hugging Face refuses a download because HF_TOKEN is unset or rejected or because its account lacks access to a gated repository, THEN THE Source_Checker SHALL report the line as access denied and name the token variable (CIVITAI_TOKEN or HF_TOKEN).
7. IF a host does not answer within 30 seconds, the connection fails, or the host answers with a rate-limit or server error, THEN THE Source_Checker SHALL retry the check up to 2 times, at least 5 seconds apart. After the last failed attempt THE Source_Checker SHALL report the line as unreachable, with that attempt's error.

### Requirement 25: Security, privacy and environment isolation

**User Story:** As the repo owner, I want the new tools to stay local, keep my persona private and leave ComfyUI's Python untouched, so that pushing to GitHub and running on a rented host stay safe.

#### Acceptance Criteria

1. THE Batch_Generator and Export_Tool SHALL open network connections only to the ComfyUI_API and the Ollama_API, and only at loopback addresses (127.0.0.0/8 or ::1).
2. THE Training_Kit SHALL open network connections only to the ComfyUI_API and the Ollama_API at loopback addresses, and, for downloads only, to GitHub, PyPI, the PyTorch package index, the npm registry, Hugging Face and, while the Training_Kit builds the ai-toolkit UI, `binaries.prisma.sh` (Prisma's engine host) and `fonts.googleapis.com` and `fonts.gstatic.com` (Google Fonts, fetched by Next.js at build time), including the file hosts that these services redirect downloads to.
3. THE Training_Kit, Disk_Estimator and Source_Checker SHALL take HF_TOKEN and CIVITAI_TOKEN only from the environment variables of those names, with no command-line option, prompt or configuration-file entry for either token.
4. THE Repo SHALL, through `.gitignore`, make git ignore an `influencer/` folder at the Repo root (where the Runbook tells Mac users to keep a local Influencer_Root), `models/loras/characters/`, and the Check_Suite's local environment and its pytest, Hypothesis and Python bytecode caches, while matching no file that git tracks.
5. WHERE RCLONE_REMOTE is set, THE Backup_Script SHALL copy every Influencer_Root at its default location, and `models/loras/characters/`, with `rclone copy`. It SHALL skip any of these folders that does not exist, and copy each one into its own remote folder that no other copy made by the Backup_Script writes into.
6. WHILE running on the Instance, THE Batch_Generator, the Export_Tool and the Training_Kit's Python helpers SHALL run with the ComfyUI_Env interpreter, using only the Python standard library, PyYAML, Pillow, NumPy and the ffmpeg command-line tools (`ffmpeg`, `ffprobe`), and leaving the packages and versions installed in the ComfyUI_Env unchanged.
7. THE Batch_Generator, Export_Tool, the Training_Kit's Python helpers, Config_Checker, Disk_Estimator and Source_Checker SHALL run on Python 3.10, 3.11, 3.12, 3.13 and 3.14.
8. THE Disk_Estimator and Source_Checker SHALL open network connections only to the Ollama_API, Hugging Face, Civitai, the Ollama library and the hosts named in the URLs of the Config_Lists lines they process, including the file hosts that these services redirect downloads to.
9. THE Training_Kit, Disk_Estimator and Source_Checker SHALL let the values of HF_TOKEN and CIVITAI_TOKEN appear only in process environments and in requests to the token's own service (HF_TOKEN to Hugging Face, CIVITAI_TOKEN to Civitai). The values SHALL never appear in a process argument list, standard output, standard error, or a file written by these tools or the processes they start.
10. IF the running Python interpreter is older than 3.10, or cannot import PyYAML, Pillow or NumPy when the command needs it, THEN THE Batch_Generator, Export_Tool, the Training_Kit's Python helpers, Config_Checker, Disk_Estimator and Source_Checker SHALL exit with a non-zero status and an error message naming the running Python version or the missing package, before writing any file or opening any network connection.

### Requirement 26: Offline config checks

**User Story:** As the maintainer, I want an offline check of every config file, so that a wrong tag, a missing bundled file or a leaked token is caught on my Mac before I push.

#### Acceptance Criteria

1. IF a `wf:` tag in an active line of the Config_Lists (a line that is neither blank nor starts with `#` after leading whitespace) lists an id that matches no Catalog id exactly, with spaces ignored and letter case significant, or lists an empty id (as in `wf:` or `wf:a,,b`), THEN THE Config_Checker SHALL report a failure that names the id.
2. IF the Catalog or the Config_Lists hold an active line that breaks one of these rules, THEN THE Config_Checker SHALL report a failure that names the field:
   - Field count, where a trailing `|` adds an empty field: 4 in the Catalog, 6 in models.list, 1 to 3 in nodes.txt, and 1 or 2 in ollama-models.txt.
   - models.list kind: `workflow`, `checkpoint`, `lora`, `vae`, `vae_approx`, `clip`, `clip_vision`, `text_encoder`, `diffusion`, `upscale`, `embedding`, `controlnet`, `detection`, `sams`, `model_patch`, `audio_encoder` or `latent_upscale`.
   - models.list source: `hf`, `civitai`, `url` or `bundled`.
   - hf source id: `repo` or `repo::path`, where `repo` is `<owner>/<name>` made only of letters, digits, `-`, `_` and `.`, and `path` is a non-empty relative path without `..` segments.
   - civitai source id: one or more digits, optionally followed by `::` and a non-empty member name.
   - url source id: `https://` followed by a host name, with no whitespace.
   - bundled source id: a relative path to a regular file inside `config/` that git tracks.
   - models.list filename: non-empty and without `/`. Subdir: empty, or a relative path without empty, `.` or `..` segments.
   - Tag field (field 6 in models.list, 3 in nodes.txt, 2 in ollama-models.txt): empty or starting with `wf:`. No other field starts with `wf:`.
3. IF two active models.list lines have the same install path but a different source or source id, THEN THE Config_Checker SHALL report a failure. A line's install path is `models/<subdir>/<filename>`, with the Provisioner's default folder for the kind in place of an empty subdir, or the ComfyUI workflows folder plus the filename for kind `workflow`.
4. IF two Catalog lines share an id, a Catalog id is empty or contains characters other than lowercase letters, digits and hyphens, an `in_all` value is neither `yes` nor `no`, or a `min_comfyui` value is neither empty nor 2 or 3 groups of digits joined by dots (such as `0.30.0`), THEN THE Config_Checker SHALL report a failure that names the field.
5. IF an active nodes.txt URL is not `https://github.com/<owner>/<repo>`, where `<owner>` and `<repo>` are made only of letters, digits, `-`, `_` and `.` and nothing follows `<repo>` except an optional `.git`, or two active nodes.txt lines have the same `<repo>` when letter case and a `.git` suffix are ignored, THEN THE Config_Checker SHALL report a failure that names the URL.
6. IF a file in `config/patches/` is not valid JSON, or its name is not `<filename>.patch.json` for the filename of an active models.list `workflow` line whose filename does not end in `.zip`, THEN THE Config_Checker SHALL report a failure that names the patch file.
7. IF an active NSFW_Set line's `wf:` tag lists any id other than `influencer-nsfw`, an active NSFW_Set line of kind `lora` has an install path (criterion 3) outside `models/loras/nsfw/`, or an active models.list line outside the NSFW_Set has an install path inside `models/loras/nsfw/`, THEN THE Config_Checker SHALL report a failure.
8. IF a term from the committed term list for minor-coded, age-slider or face/head/body-swap resources matches a line of the Config_Lists, comment lines included, or a node pack name in a Render_Template's node-class-to-pack map, THEN THE Config_Checker SHALL report a failure that names the term. A term matches when its words appear as consecutive whole words in any letter case, where every character other than a letter or digit separates words: `face swap` matches `Face_Swap` and `face-swap`, but not `faceswap`.
9. IF a file that git tracks, including a newly staged file, contains a string in one of these token formats, THEN THE Config_Checker SHALL report a failure with the file path and line number, and leave the matched string out of its output:
   - Hugging Face: `hf_` followed by 30 or more letters or digits
   - GitHub: `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` followed by 36 letters or digits, or `github_pat_` followed by 22 or more letters, digits or underscores
   - Civitai and Vast.ai: exactly 32 or exactly 64 hexadecimal characters with no letter or digit on either side, on a line that also contains `civitai`, `vast`, `token` or `bearer` in any letter case
10. IF git tracks a file other than the committed examples that has one of these forms, whether or not the file passes the matching Reader's validation, THEN THE Config_Checker SHALL report a failure:
    - a YAML file with the top-level structure of a Persona_File, Content_Calendar or Prompt_Sheet, as the matching Reader defines it
    - a CSV file whose header row names every column that the Content_Calendar CSV format of Requirement 5, criterion 14 requires
11. IF a Render_Template breaks one of these rules, THEN THE Config_Checker SHALL report a failure that names the item:
    - The file is valid JSON.
    - Every placeholder in the graph is declared in the metadata.
    - Every node class in the graph has an entry in the node-class-to-pack map.
    - Every node pack in that map is ComfyUI's built-in node set or a pack that a nodes.txt line in the Selection for the template's Catalog id installs.
    - Every model file in the metadata is installed by a models.list line in the Selection for the template's Catalog id, at an install path (criterion 3) whose part below the kind's default folder equals the name the template uses; for example, a LoRA at `models/loras/Krea2/x.safetensors` is named `Krea2/x.safetensors`.

    The template's Catalog id is `influencer` for the Still_Template, Edit_Template, Finish_Template and Refine_Template, and `influencer-video` for the Reel_Template and Ref_Reel_Template.
12. IF the Persona_Reader rejects the Persona_Example for a reason other than the file-existence check of Requirement 4, criterion 7, the Calendar_Reader rejects the example Content_Calendar when read with the Persona_Example, or the example Test_Grid, as the Batch_Generator's grid stage reads it, does not hold exactly 20 prompts each with an integer seed, THEN THE Config_Checker SHALL report a failure for each error, with the file, the field and the reason.
13. WHEN the Config_Checker computes the Selection for a Catalog id, for `global` or for a full run, THE Config_Checker SHALL produce the same files, node packs and Ollama models, in the same order, as the Provisioner's read-only Selection mode prints for the same Catalog and Config_Lists (equivalence property).
14. THE Line_Printer SHALL write every valid line record so that the Line_Reader reads back an equal record (round-trip property). A line record is valid when criterion 2 allows its field count, its first field is non-empty and does not start with `#`, and no field contains `|`, a line break, or leading or trailing whitespace.
15. THE Config_Checker SHALL report every failure that any check finds in one run, each with the file path, the numbers of the lines it concerns, and the rule it breaks.
16. WHEN the Config_Checker finishes a run, THE Config_Checker SHALL exit with status 0 for a run with no reported failure and with a non-zero status for a run with one or more reported failures.
17. IF git is not installed, the Repo checkout is not a git work tree, the Catalog or one of the Config_Lists is missing, the committed term list is missing or lists no terms, or the Noncommercial_List is missing or lists no licence identifiers, THEN THE Config_Checker SHALL report a failure that names each missing item.
18. IF an active models.list or ollama-models.txt line whose `wf:` tag lists an Influencer_Id (shared lines included) does either of these, THEN THE Config_Checker SHALL report a failure that names the line and, when a `# licence:` line exists, the licence:
    - has no `# licence:` line in the comment block that Requirement 2, criterion 4 places for it
    - has a `# licence:` line that fails the Licence_Gate: it names a licence on the Noncommercial_List, or Civitai commercial-use permissions without `Image`

### Requirement 27: Check suite

**User Story:** As the maintainer, I want one command that runs every check and test on my Mac, so that each change is verified before it reaches an instance.

#### Acceptance Criteria

1. WHEN the maintainer runs the Check_Suite command without arguments, on macOS with the Mac's bash 3.2 or on Linux, THE Check_Suite SHALL run four steps: the Config_Checker, the Python unit and property-based tests, `bash -n` on every shell script, and shellcheck at its default severity on every shell script. A shell script is a file in the Repo checkout that git tracks, or that is untracked and not excluded by `.gitignore`, and whose name ends in `.sh` or whose first line is a `bash` or `sh` shebang.
2. WHILE the Check_Suite's local environment holds every pinned package at its pinned version, THE Check_Suite SHALL give each step the same result with or without a network connection, a GPU, a `/workspace` folder, or a ComfyUI_API or Ollama_API answering on the host.
3. WHEN the Check_Suite starts and its local environment does not hold every pinned package at its pinned version (for example on the first run, after a failed install or after a pin changes), THE Check_Suite SHALL build that environment with a Python 3.10 or later interpreter, skipping older ones such as the Mac's system Python 3.9. It SHALL install pytest, hypothesis, PyYAML, Pillow, NumPy, every other package the steps need, and all their dependencies, each at the exact version pinned in the Repo, and leave the packages of the ComfyUI_Env and of every other Python installation unchanged.
4. IF shellcheck is not installed, THEN THE Check_Suite SHALL report the shellcheck step as skipped, print a command that installs shellcheck on the host's operating system, and run the remaining steps.
5. THE Check_Suite SHALL include a property-based test for each property below (numbered requirement.criterion). Each test SHALL name its property and run at least 100 generated inputs, or every possible input where there are fewer:
   - round-trip: 4.11 Persona_File, 5.13 Content_Calendar, 5.18 CSV Content_Calendar, 6.12 Prompt_Sheet, 10.12 Manifest, 14.9 LUT, 26.14 Catalog and Config_Lists lines
   - invariant: 6.7 Identity_Paragraph, 7.2 placeholder filling, 7.5 LoRA loader chain, 11.3 Edit_Template instruction, 18.7 source Dataset left unchanged by refining, 20.5 grid Jobs of one Test_Grid prompt
   - idempotence: 6.9 disclosure line, 14.4 repeated export, 18.4 caption Trigger_Word, 19.9 checkpoint publishing
   - metamorphic: 14.5 identity LUT
   - equivalence: 5.16 CSV and YAML Content_Calendars, 26.13 Config_Checker and Provisioner Selections
   - path containment: 5.10 output paths, 21.8 platform `other` folder
   - other properties: 7.4 whole-value filling, 7.6 repeatable filling, 7.8 filling inside longer strings, 11.5 Candidate seeds, 13.4 export crop box, 16.3 Reel first-frame crop, 16.7 Reel frame count and 16.8 Reel width and height (H3_Rules)
   - 10.1 seeds: the same Week_Id, Post id, Slide index and Candidate index give the same seed in separate processes, and every seed is an integer from 0 to 4,294,967,295
   - 6.8 caption limits: an Instagram caption passes exactly when its full caption text has at most 2,200 UTF-16 code units and no more words starting with `#` than the Persona_File's hashtag limit (default 5; a higher limit counts as 5)
   - Safety_Guard: a term from the committed minor-coded term list is rejected as a whole word in any letter case, and accepted when it appears only inside a longer word. A number written with an age word is rejected exactly when the number is below 25.
6. THE Check_Suite SHALL include a regression test that runs the Provisioner's read-only Selection mode for every Existing_Id, for `global` and for a full run. The test SHALL fail when the Selection of an Existing_Id or of `global` differs from the Selection that the Selection_Snapshot records for that run, or when the full-run Selection differs from the union of the Selection_Snapshot's full-run lines and the `influencer` Selection, in any file, node pack or Ollama model. The test SHALL name the run and each item that differs.
7. WHEN the last step ends, THE Check_Suite SHALL print one line per step with the step name and its result (passed, failed or skipped). It SHALL then exit with a non-zero status if any step failed, and with status 0 otherwise. A skipped step does not count as failed.
8. IF a step fails, THEN THE Check_Suite SHALL still run every remaining step.
9. IF the Check_Suite finds no Python 3.10 or later interpreter, or a pinned package fails to install, THEN THE Check_Suite SHALL report the Config_Checker and Python test steps as failed. The report SHALL state that Python 3.10 or later is required, or name the package that failed to install.

### Requirement 28: Documentation

**User Story:** As the creator, I want a runbook for the whole pipeline and a weekly checklist, so that each session follows the same steps and ends with the instance stopped.

#### Acceptance Criteria

1. THE Runbook SHALL give one heading per pipeline stage, in this order: writing the Persona_File, closed-model character design (manual), Dataset curation, Character_LoRA training and checkpoint choice with the Test_Grid, the weekly session, export, QC, and manual publishing from the Publish_Sheets. Each stage's section SHALL name the commands that run the stage, or state that the stage is manual, and name the files that the stage reads and writes.
2. THE Runbook SHALL give a numbered weekly session checklist. Before the checklist, it SHALL list the one-time Mac prerequisites: a Python 3.10 or later environment with the packages that the `plan` stage imports, the Persona_File's Ollama models in the Mac's Ollama, and a local Influencer_Root in the `influencer/` folder at the Repo root. Each step SHALL name its command or state that the step is manual. The checklist SHALL contain at least these steps, in this order, and SHALL end with stopping the Instance:
   - write the week's Content_Calendar on the Mac
   - run the `plan` stage on the Mac against the Mac's local Ollama
   - review and edit the Prompt_Sheet
   - start the Instance
   - copy the Persona_File, Content_Calendar and Prompt_Sheet into the Instance's Influencer_Root
   - run the `render` stage
   - record the Picks in the Prompt_Sheet
   - run the `finish` stage
   - run the `reels` stage, in weeks with reel Posts
   - run the export stage
   - review the QC_Sheets
   - copy the week's Output_Root files, Prompt_Sheet and Manifest from the Instance to the Mac
   - stop the Instance by hand
3. THE Runbook SHALL give a Dataset shot list of person images. Each entry SHALL give an image count, an angle, an expression, an outfit and a lighting condition. The counts SHALL add up to 30–40 images. Across all entries the list SHALL cover the 5 angles front, left and right three-quarter, and left and right profile, at least 4 expressions, 2–3 outfits and at least 3 lighting conditions. The shot list SHALL state that curation keeps 25–35 of the images, the range in which the Training_Kit's Dataset check gives no warning. THE Runbook SHALL also give a separate Location_Shots list with at least 1 empty shot (no person) of each recurring location, and state that Location_Shots are kept outside the Dataset, so that the Training_Kit writes no Trigger_Word caption for them.
4. THE Runbook SHALL give a disk table with these rows, in GB to one decimal place and with the date of measurement:
   - the total download size that the Disk_Estimator reports for each Influencer_Id and for each training base model
   - the total download size that the Disk_Estimator reports for `all` (the ids of Requirement 1, criterion 8), which includes the new `influencer` files (about 102 GB per Appendix A)
   - a disk size to rent of at least the Disk_Estimator's combined total for the four Influencer_Ids, plus the `krea/Krea-2-Raw` base model, plus 50 GB
5. THE Runbook SHALL include the Instagram disclosure checklist, with the same items in the same order as the Publish_Sheet's disclosure checklist. It SHALL also summarise Instagram's nudity rules, with a link to Instagram's Community Guidelines and the date the rules were checked.
6. THE Runbook SHALL give the SSH tunnel command for the ai-toolkit UI. The command SHALL forward the same port as the command that the Training_Kit prints when it starts the UI, with the Instance's SSH host and port as placeholders.
7. THE Runbook SHALL state that driving clips and voice clips must be the user's own or licensed.
8. THE Runbook SHALL give a licences section. The section SHALL state that the licence and Civitai commercial-use permissions of every model used for monetised posts must be checked before the first monetised post. It SHALL give the Source_Checker command whose report lists them and the Licence_Gate results for the four Influencer_Ids. It SHALL also list the following, each with the date it was checked:
   - every condition that the licence or Civitai permissions of a resource installed for an Influencer_Id set on commercial use of outputs, for example:
     - the Krea 2 Community License's US$1,000,000 annual revenue ceiling (affiliates included), its content-filter requirement and its AI-disclosure duty
     - Civitai's credit requirements
   - the acceptable-use limits found in those licences and their acceptable use policies, for example the Krea 2 Acceptable Use Policy's bans on:
     - non-consensual intimate imagery and deceptive deepfakes of real people
     - content that is obscene under applicable law
     - circumventing Krea's safety measures
     - presenting outputs as human-made to deceive
9. THE Runbook SHALL list these open items, each with the check that resolves it:
   - the Krea 2 Identity Edit and Krea2-Reference LoRA hosts
   - whether H3 LoRA training fits one 80 GB GPU
   - the H3 maximum frame count
   - whether Krea 2 LoRAs trained with ai-toolkit load in ComfyUI without missing-key warnings
10. THE Repo SHALL record the feature as v7 in CHANGELOG.md, with a v7 entry above the v6 entry and the current-version line set to v7. The entry SHALL be dated YYYY-MM-DD and list every Influencer_Id and tool that the feature adds.
11. THE Runbook SHALL state that the Persona is fictional and aged 25 or older. It SHALL state that real-person likeness, face/head/body swaps, minor-coded resources and age sliders are excluded from the Repo and must not be used with it.
12. THE Runbook SHALL state that HF_TOKEN is set only as an environment variable, and never in ai-toolkit's settings page, a `.env` file or any other file.
13. IF a Runbook section or step depends on a command, stage or Influencer_Id that the Repo does not yet contain, THEN THE Runbook SHALL mark it as not yet available and name the delivery phase that adds it.
14. THE Runbook SHALL document the Content_Calendar CSV format of Requirement 5, criterion 14: every column, which columns are required, how a list value is written in one cell, and one example row for each Post type (`single`, `carousel`, `story` and `reel`).

## Appendix A: Resource inventory

This is the starting set, verified 2026-09-28 unless marked. "Shared" lines already exist and gain the new tag.

| Id | Resource | Source | Size |
|---|---|---|---|
| influencer | Krea 2 Raw bf16 | HF `Comfy-Org/Krea-2::diffusion_models/krea2_raw_bf16.safetensors` | 26.28 GB |
| influencer | Krea 2 turbo LoRA, rank 64 | HF `Comfy-Org/Krea-2::loras/krea2_turbo_lora_rank_64_bf16.safetensors` | 0.47 GB |
| influencer | Qwen3-VL-4B bf16 encoder | HF `Comfy-Org/Krea-2::text_encoders/qwen3vl_4b_bf16.safetensors` | 8.88 GB |
| influencer | Wan 2.1 VAE fp32 | HF `Kijai/WanVideo_comfy::Wan2_1_VAE_fp32.safetensors` | 0.51 GB |
| influencer | Wan 2.1 VAE upscale2x (sharp decode) | HF `spacepxl/Wan2.1-VAE-upscale2x::Wan2.1_VAE_upscale2x_imageonly_real_v1.safetensors` | 0.51 GB |
| influencer | Lenovo UltraReal Krea 2 (shared with krea2) | HF `Danrisi/Lenovo_Krea2::lenovo_krea2_3000.safetensors` | 0.11 GB |
| influencer | Look LoRAs: NiceGirls 3075498, Realistic Snapshot 3084537, Add Micro Details 3298811, Realistic Skin Texture 3167109, Emotion Enhancer 3105008 | Civitai versions (free) | 0.99 GB |
| influencer | Krea 2 realism guide workflow (zip) | Civitai 3092832 (free) | small |
| influencer | Qwen-Image-Edit-2511 bf16 (Edit_Template) | HF `Comfy-Org/Qwen-Image-Edit_ComfyUI::split_files/diffusion_models/qwen_image_edit_2511_bf16.safetensors` | 40.86 GB |
| influencer | Qwen2.5-VL-7B bf16 encoder (Edit_Template) | HF `Comfy-Org/Qwen-Image_ComfyUI::split_files/text_encoders/qwen_2.5_vl_7b.safetensors` | 16.58 GB |
| influencer | Qwen-Image VAE (shared with krea2, krea-style and edit-2509) | HF `Comfy-Org/Krea-2::vae/qwen_image_vae.safetensors` | 0.25 GB |
| influencer | VOSR2 weights, fetched at install | HF `CSWRY/VOSR` (target folder to confirm) | ~7 GB |
| influencer | Face detection model for the detailer | chosen in design | < 0.1 GB |
| influencer | Nodes: RES4LYF and rgthree (shared); a face detailer pack (Impact Pack or Forbidden Vision, chosen in design); `ylchen333/ComfyUI-VOSR2`; `lunaaispace-eng/ComfyUI-DeGrid`; `spacepxl/ComfyUI-VAE-Utils` | GitHub | — |
| influencer | `qwen3:8b` (shared with global) | Ollama | 5.2 GB |
| influencer-video | minimax-h3 weights, turbo LoRA and taeh3 (shared) | existing lines | 63 GB |
| influencer-video | H3 look LoRAs: Amateur Hour 3317385, IG/TikTok Aesthetics 3322046 | Civitai versions (free) | to be measured |
| influencer-video | ffmpeg; optional `numz/ComfyUI-SeedVR2_VideoUpscaler` for Reel upscaling | apt / GitHub | — |
| influencer-nsfw | PhotoReal BetterNudes 3066702, Krea2 Filter Bypass [Fedor] 3089754, Mystic XXX H3 v4 3266628, H3 NSFW Unlocked V1.1 3345855 | Civitai versions (free), installed under `loras/nsfw/` | 0.23 GB, plus the H3 files |
| influencer-nsfw | `dolphin3:8b` (shared with krea2) | Ollama | 4.9 GB |
| character-design | `krea2_lineart_edit_v1`, `krea2_lineart_coarse_edit_v1`, `lineart_example.json` | HF `Cierpliwy/krea2_lineart_edit` | to be measured |
| character-design | `QuadView_krea2_v1`, `DynamicCharacterSheet_krea2_v1`, `workflows/*.json` | HF `Alissonerdx/CharacterSheet` | to be measured |
| character-design | `ostris/ComfyUI-Krea2-Ostris-Edit`; `comfyui_controlnet_aux` (shared) | GitHub | — |
| training (not a catalog id) | `krea/Krea-2-Raw` (diffusers, gated); `MiniMaxAI/MiniMax-H3` (experimental) | HF | Disk_Estimator |
| commented out | `lbouaraba/comfyui-krea2edit` + `krea2_identity_edit_v1_2` (LoRA host unconfirmed: HF returns 401, only mirrors found); `KonokoAz/ComfyUI-Krea2-Reference` + its LoRA (source unconfirmed) | — | — |
| commented out | Licence_Gate failures (Civitai commercial use without `Image`): Aesthetic Amateur Photo Krea V4 3284331 (RentCivit only) and Smartphone Photography Slider 3151907 (RentCivit, Rent), both `influencer`; SNOFS Krea 2 v1.4 3290120 (RentCivit only), `influencer-nsfw` | Civitai versions | — |
| excluded | Krea2FilterBypass 2-vector 3066812 and 3-vector 3067151 (paid) | — | — |

The estimated `influencer` total is about 108 GB. About 102 GB of that is new. The rest, 5.6 GB, is already installed by `krea2` and `global`: the Lenovo Krea 2 LoRA, the Qwen-Image VAE and `qwen3:8b`.
