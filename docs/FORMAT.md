# AVANIM v1

`.avanim` is a ZIP, read as data without extraction or executable content.

- `manifest.json`: UTF-8, version=1; id `[A-Za-z0-9_-]{1,64}`; Unicode name; profile ID; mode `button` or `slider`; scope describes the export selection; loop; duration in seconds; sampleRate; frameCount.
- bones: ordered array `{name, parent, rest:[12 floats]}`. Rest is a row-major 3×4 affine matrix in the current avatar model units. Parent and rest are checked against the loaded avatar. Unknown control bones can be ignored once at least one exported bone matches; author rigs should match the avatar rig.
- `pose.bin`: little-endian float32, frame-major / bone-major / ten values: translation xyz, quaternion xyzw, scale xyz. This is the local delta after the rest matrix: localPose = localRest × TRS(delta). Constraints and NLA are evaluated in Blender before baking. Positive nonzero scale is required. Armature object motion relative to the first exported frame is baked into root bones; it affects visual rendering only.
- visibility: JSON object mapping existing mesh names to arrays of frameCount booleans. Sampled with step/hold semantics. Only explicitly selected or animated visibility properties are exported.
- audio: null, or `{file:"soundtrack.wav", cues:[{name,start,end,...}]}`. WAV is PCM16 stereo mixed by Blender over the scene frame range. Cues preserve timing/source names for inspection; runtime plays the mix, not individual source files. Frame trim, volume envelopes and overlap are already baked.
- source: application / Blender version, resolved startFrame / endFrame / source FPS. Add-on 1.0.1 also records rangeMode and rangeSource for inspection; these optional fields do not change the v1 runtime format. Automatic export uses active Action keys (or its explicitly enabled manual frame range), falling back to enabled NLA clips, visibility keys, then the scene range. EVENTS-only export uses visibility keys instead of armature keys. All pose, visibility and audio times use the same resolved interval, including the final authored key/hold.

Pose sampling interpolates translation/scale linearly and quaternions on the shortest arc. Float parameters are independent layers, with their saved normalized time; a timed emote is applied afterward. In runtime 1.1.0, any package containing bones owns the full body; missing bones in legacy sparse packages use rest pose instead of normal game locomotion. Events-only packages do not take ownership of bones. Full skeletal exports always sample the entire rig, including unkeyed bones. Stop restores the underlying layer. Visibility overrides do not write to base mesh settings.

A sound worker owns each audio line. The action clock waits for decoder readiness, then pose and audio start from the same phase. Seeking pauses by default. Game pause freezes playback; ordinary action screens do not pause the world. Volume follows Minecraft Master × Players.

Limits: up to 600 seconds, 5–60 sample FPS, 512 exported bones, 64 MiB pose data, 64 MiB WAV, 128 files per library and 256 MiB decoded package data per reload. Existing avatar geometry is not part of an action package.


## Editable import (add-on 1.1.0)

The importer builds a new per-frame Action on a matching avatar rig, using Bone.convert_local_to_pose for basis conversion. Bone aliases, rest transforms and hierarchy are validated before animation assignment. Quaternion signs are kept continuous; transforms use LINEAR key interpolation. Visibility uses CONSTANT keys, including the final frame so automatic export retains a hold.

Imported Actions store avatar_action_id, avatar_action_name and avatar_bridge_import metadata. The scene retains import settings for events-only clips. Re-export uses stable identity; New Action ID explicitly forks it. The mixed WAV is packed into the .blend and exposed as an editable VSE strip. Original cue metadata is retained while unchanged; edited mixes use current timeline cue metadata. Only existing geometry is addressed.

No AVANIM version change was needed. Automatic/manual/scene ranges from add-on 1.0.1 remain available. Tests and game/Blender playback checks for 1.1.0 were not run.


## Base-state files

AnyModel 2.8.0 authored locomotion defaults add two optional v1 metadata fields:
`authoredLocomotion: true` retains the clip's coordinated arm motion during
ordinary movement; live attack/use poses still take precedence. `gaitClearance`
is an array of frameCount finite values in [0, 0.5], expressing the support-plane
clearance as a fraction of average leg length. Walk uses zero; run can contain
a short airborne phase. Clearance interpolates with the pose and across gait
transitions, allowing visible footwear and other humanoid proportions to share
the same animation. Blender avatar export preserves these fields from templates.
Missing fields preserve the previous gameplay adaptation behaviour.

In AnyModel 2.8.1 the two base roles `attack_empty` and `attack_item` are sampled
by native swing progress instead of a separate wall clock. Their torso/arm
rotations overlay the current base pose; pelvis and legs remain in locomotion.
`useOwner` (RIGHT by default) supplies the authored arm for mirroring. Replacing
these role files does not require a special flag; `authoredAttack` is optional
descriptive metadata on the shipped defaults. First-person grip correction
tracks and the editor timeline use the same normalized swing phase.

The client loads each base state directly from config/AnyModel/avatar_actions/<profile>/base_animations/<state>.avanim. The filename selects the state; internal action ID/name can stay unchanged when replacing it. All standard AVANIM v1 pose fields remain the same. Shipped defaults additionally use optional baseGameplay=true for live Minecraft item/aim/ground/root adaptation; plain authored exports omit it and control the full skeletal pose. Base playback uses skeletal data only, with menu actions layered afterward. Removing an initialized base file produces T-pose rather than restoring a bundled default. The user edits/replaces each file directly and uses the movement settings reload button.


Authored AVANIM model-space zero is the avatar support plane. Mounted rendering adds the rider vehicle-attachment offset in world space before avatar scaling so zero maps to the vehicle seat, not Steve's lower entity origin. Authored root translations are retained, with no per-frame pelvis or bounds re-centering. Optional origin: floor marks newly baked gameplay defaults; legacy baseGameplay packages without it retain the old seated rest-hip compatibility offset. Ordinary Blender packages always use floor origin without needing this field.


## MCAVATAR v1 / NZM4 (AnyModel 2.0)

One data-only ZIP per config/AnyModel/custom/<folder> containing avatar.json (version=1, id=custom_[a-z0-9_-]{1,48}, name, model=model.nzm, default visibility by mesh name), model.nzm, textures/*.png and base_animations/<state>.avanim. Resource paths are validated and code is never loaded from the package. Optional outline=true enables the old contour; default is false for alpha-card compatibility.

NZM4 uses the NZM3 header/tables and row-major affine bone rest matrices. Each expanded triangle vertex is 8 little-endian float32 position/normal/UV values, followed by 4 uint16 bone indices and 4 uint8 weights summing to 255 (44 bytes). Bones are unique and topologically ordered. Old NZM1–3 retain uint8 indices. Custom exports support 512 bones, 200K triangles, 128 PNG textures (8192px per side and 64M pixels total). Model payload is bounded at 128 MiB; package at 512 MiB.

Geometry is converted from Blender world Z-up to engine Y-up with facing inferred from the humanoid arms; support zero and a configured height define scale. Exported rest rotations remain normalized. The rig stores minecraft_avatar_export_space, including conversion/unitScale and engine rest transforms, so later AVANIM exports/imports use the same coordinate basis and scale. Native bone names are preserved; avatar_bone aliases select humanoid names.

Base defaults are retargeted from bundled CC0/current-mod templates by global rotation deltas and limb rest-direction calibration, using target bone lengths. Source Hips displacement scales to target hip height. On package replacement the client updates only original base files whose hashes still match its saved .custom-defaults.json index; edited/deleted files remain untouched. Authored replacements still use complete local TRS, profile/rest validation and live replacement.


## Local cross-avatar preparation (AnyModel 2.1.4)

The v1 format is unchanged. Local import may bake a full humanoid clip to target-native bones/rest transforms on the asset worker. Canonical body/finger aliases and rest-direction calibration preserve target proportions; Hips global XYZ displacement is scaled by the target/source leg-length ratio. Timing, rootMotion flags, audio/cues and safe credits remain intact; unmatched visibility tracks are omitted. Optional humanoidTransfer metadata records sourceProfile, matchedBones and movementScale. Source archives are not modified. The generated cache key includes source bytes, target rig/mesh names, profile and conversion revision. Network receivers still use strict native validation; publishers send converted native action files. Shared local clips live in config/AnyModel/animations, with avatar-local IDs taking precedence.
