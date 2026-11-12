# 2.3.5 — Authored attack templates

- Replace attack_empty and attack_item with calibrated CC0 Punch_Cross / Sword_Attack clips, retaining bind orientation and an early impact phase.
- Preserve authoredAttack/useOwner when exporting the 16 base templates. Runtime 2.8.1 samples these clips by native swing progress and overlays upper-body rotations.
- Existing locomotion, MMD and fast-audio export remain unchanged; no third-party addon changes.

# 2.3.4 — Complete authored locomotion templates

- Replace walk, backward walk and run with complete calibrated Quaternius CC0 cycles; preserve bind orientations, hip movement, coordinated arms and exact loop endpoints.
- Retain authoredLocomotion and normalized gaitClearance when building a new avatar. Templates remain ordinary editable AVANIM v1 files, compatible with AnyModel 2.8.0 contact/arm adaptation.
- Keep fast audio export and MMD registration unchanged; no third-party addon changes.

# 2.3.3 — Fast sequencer audio export

- Mix ordinary sequencer sound in a temporary audio-only scene, preserving copied strips, Meta hierarchy, offsets/trims, animation, packed sound and native Blender audio processing. Remove avatar geometry and non-audio strips only from the temporary scene; always clean it up.
- Keep original-scene fallback for spatial speakers, nested scene strips, scene drivers, frame handlers and rigid bodies. Report separate pose/audio/packing timings and audio sampling path without changing AVANIM v1.
- Actual Conqueror MP3: short native/isolated PCM matches byte for byte; full isolated mix takes under one second. Full 7002-sample export with audio takes 32 seconds in the recorded background check, preserving previous pose.bin exactly. Additional packed/trimmed/nested/multitrack/animated-volume tests preserve source data and PCM.

# 2.3.2 — MMD panel survives Blender startup

- Fix partial addon registration on a real Blender restart: orphan physics-preview cleanup previously accessed bpy.data during RestrictBlend registration and aborted before MMD classes/properties registered.
- Defer cleanup to a one-shot timer/load_post; guard restricted data access and cancel the pending timer on unregister. Preserve existing physics configuration and animation data.
- Validate restricted registration/disable/re-enable and actual Blender --addons startup, in addition to live repair. Restore the MMD panel/operators without restarting or re-registering the user's physics data.

# 2.3.1 — Direct baked-Action export and fewer redundant keys

- Certified baked MMD Actions use a direct F-curve sampler without geometry/depsgraph evaluation on every sample. Standard inheritance reduces directly to keyed bone basis; special inheritance and normalized roots use matrix conversion. Procedural rigs, drivers, NLA and frame handlers retain evaluated fallback.
- Stream pose rows into the binary buffer instead of a nested Python list of every pose. Preserve AVANIM v1 metadata, visibility and scene/imported sound handling; add sampling-path metadata and progress output.
- New retargeted Actions reduce redundant linear keys using bounded interpolation error: 20 µm local translation in world units, 1e-5 quaternion components and scale. Existing Actions are not changed by export.
- Complete Conqueror checked against native Blender evaluation at integer and fractional frames, including normalized export coordinates. Full 7002-sample export retains the 233.367-second duration. Actual native geometry checks, counts and timing are in AnyModel_Addon_2.3.1_Verification.

# 2.3.0 — Selected avatar + VMD → editable Action

- Primary workflow is one import button: select the avatar rig or bound mesh, choose VMD, receive a normal editable Blender Action on that exact rig. Pose Mode is restored when used; previous Actions remain with Fake User.
- Generate a temporary mesh-free PMX humanoid from target measurements, with standard MMD torso, finger, twist and leg/toe IK hierarchy. No artist reference skeleton or PMX/PMD selection is required; all temporary objects, armatures, meshes and source Actions are removed after baking, including on failures.
- Read skeletal frame range from the VMD, use 30 FPS and retain root travel XYZ. The source decoder is the installed MMD Tools/CATS, unchanged and external to this ZIP.
- Keep the manual source/target/reference route behind an Advanced toggle. Default panel shows the selected avatar, Add VMD and Export buttons.
- Verified full 7001-frame Conqueror with editable keys and no helper leftovers, ordinary IK motion with reach limits, selected renamed/scaled/rotated target, invalid input/failure restoration and addon lifecycle.

# 2.2.0 — MMD → Humanoid converter

- New AnyModel sidebar panel: reference PMX/PMD import, VMD import, source/target armatures, automatic and editable bone mappings, complete-range Action baking and existing AVANIM export.
- Evaluated IK, root travel in XYZ, body-axis alignment, limb rest-pose calibration, proportional movement and separate finger phalanges. New Actions retain previous Actions with Fake User; reuse the existing setup restore workflow.
- Supports external MMD Tools and CATS operator namespaces, including explicit VMD file lists and new-Action import. No third-party addon source is changed or bundled. Mesh-free reference imports can complete a missing upper-body-2 joint before VMD import.
- Sidebar and animation import/export labels consistently use AnyModel. Existing identifiers, formats, avatar physics settings and all 16 base templates remain compatible.
- Verified actual WAVEFILE motion and sound, public PMD/VMD import operators, a renamed/scaled/rotated target rig, Minecraft playback, and the complete IA Conqueror full-key motion. Detailed local results are in AnyModel_2.7_Verification.

# 1.1.0 — World viewing, complete body actions, editable import

- Added Import Avatar Action in the sidebar and File → Import, with editable full-rig transforms, constant visibility keys and a packed VSE soundtrack. Stable ID/name and import metadata support replacement exports. Old Actions remain with Fake User. Previous constraint/driver/NLA/audio mutes can be restored explicitly.
- Skeletal export always samples every rig bone, including unkeyed bones; old OVERLAY enum values are promoted to FULL. EVENTS remains a bone-free parameter mode. AVANIM v1 and 1.0.1 automatic ranges are preserved.
- Minecraft adds Watch in World and switches first-person skeletal playback to a visible third-person camera. Screen closure does not stop playback. Legacy sparse skeletal packages replace unspecified bones with rest pose instead of inheriting locomotion.
- Added a curled seated pose: pelvis support, knees drawn up and together, arms around legs. It is available through the Sit hugging knees button (movement/jump stands up) and as the rider fallback for boats/minecarts/other entities with shouldRiderSit(). Mount attachment anchors the body; item use may override arm hugging.
- Java assembly and Python syntax compilation only; no tests, Blender imports/exports or game sessions were run for this version.

# 1.0.1 — Automatic Action duration

- Automatic export uses active armature Action key bounds, filtering layered Action curves by the assigned slot.
- Repeated final keys are retained; explicitly enabled Action manual ranges retain authored holds and clip boundaries.
- Fallback bounds come from enabled NLA strips, then visibility keys, then the scene range. EVENTS mode uses visibility instead of bone Action bounds.
- Scene range is an explicit override for procedural animation, external IK targets, audio-only timelines and intentional tails.
- The resolved interval drives pose sampling, Boolean tracks, cue offsets and Blender mixdown together; source scene frame bounds are restored afterward.
- Sample counts remain duration/FPS-driven, with existing package/runtime limits unchanged. Optional source range metadata keeps AVANIM v1 compatible.
- Updated installed add-on source and ZIP. Minecraft code/JAR and existing exported actions were not changed.
- Tests were not run, as requested. No Blender session reload or export was performed.


## 2.0.0 — AnyModel
- Build AnyModel Avatar exports data-only .mcavatar with NZM4 ushort skin indices, PNG textures, humanoid aliases and 16 retargeted AVANIM base poses.
- Auto/manual humanoid mapping, editable finger mappings, target height, temporary triangle-budget reduction and source rig restoration.
- Shared normalized export space for custom model/action export and editable AVANIM import. Existing Nazuna actions retain their original space.
- New client root config/AnyModel and per-avatar custom folders; no runtime/test execution for this update, syntax/build only.


## 2.0.1 — Fit textures automatically
- Fix the export failure on Nazuna's full-size/alternate clothing textures: cap temporary image copies at a configurable maximum side (4096 default), deduplicate emitted PNGs by content and remap material/chunk texture references.
- Fit the loader's decoded 64*1024*1024-pixel budget automatically by proportionally resizing larger temporary images, preserving tiny solid colors/icons and original UVs. Rounded dimensions, including very thin images, are accounted for before resizing. Source Blender images are unchanged.
- Report unique texture count and resulting megapixels; preserve optimizer statistics in avatar.json. More than 128 genuinely unique images gets a separate atlas-specific diagnostic.
- Installed Python sources and both add-on ZIPs updated. Python source syntax compilation only; no tests or Blender/game execution.


## 2.0.2 — Missing textures do not stop export
- Replace Image.copy with explicit pixel-buffer copying into a fresh temporary image, including grayscale/RGB/RGBA conversion, source colorspace and alpha mode. Lazy data can be recovered from packed bytes or the real file without reloading the original image.
- Unreadable/unavailable images fall back to a plain material color and are reported by name. Geometry and other valid textures continue exporting. Missing-image outcomes are cached; fallback material colors remain per-material and use content deduplication afterward.
- Existing max-side resizing and aggregate texture budget fitting remain. Skip names are written to avatar.json and reported after export.
- Version/source/ZIPs installed; Python source compiled without execution. No tests, Blender exports or Minecraft runs.


## 2.0.3 — Material opacity instead of unused texture alpha
- Principled Alpha now controls exported opacity. An unlinked Alpha=1 exports opaque pixels even when an Unreal/albedo texture stores unrelated data in its alpha channel. This fixes transparent Hsin/Wuwa exports.
- Preserve explicit constant opacity, image Alpha links, separate opacity images on the same UV and constant Multiply chains. Unsupported procedural Alpha gets a material-specific bake instruction rather than silently exporting the wrong channel. Missing opacity images retain geometry with opaque fallback.
- Texture reuse includes the material opacity policy, so opaque/cutout materials sharing an image no longer accidentally reuse the wrong PNG.
- Original Blender images, materials and scene remain unchanged. Verified background Blender cases: unused alpha, shared-image/different-policy isolation, linked alpha, constant alpha, separate opacity image.
