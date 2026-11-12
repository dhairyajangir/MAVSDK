Draft only — no workflow/source publication or execution performed.

Package linux.yml over the EXISTING .github/workflows/linux.yml on the single helper branch ci/mavsdk3175-profile-validation-20261112. Package driver.py and a reviewed frozen manifest.json under .github/ci/mavsdk3175/, with its referenced payload files. Do not change default fork main. The unique workflow name avoids inherited release workflow_run names. The design includes the root-requested one-branch push trigger; no push or dispatch is authorized or performed by this draft task. Root has resumed MAVSDK validation planning; execution/publication remains outside this draft-edit task. workflow_dispatch remains in the design, with no dispatch/cancel/retry automation.

candidate_ref identifies the immutable helper/payload CARRIER, not a production candidate tree. Both real source views start at SDK6b5940 and proto df90. The candidate receives only hashed native/API-test patches and the one-file proto patch, then official regeneration produces authoritative generated source. Preliminary custom C++ and grpcio-tools-generated tracked pb files are rejected as payload. The carrier may change only the helper workflow and .github/ci/mavsdk3175/.

The workflow uses checkout@v6 with persist-credentials:false and contents:read, fixed fork identity, an exact SHA input, the pinned official amd64 image, timeout180, and upload-artifact@v4 always. Docker pull/inspect records the actual local image ID and matching RepoDigest; docker run uses that exact digest without another pull, two CPUs, runner UID and task-scoped XDG_CACHE_HOME, GRADLE_USER_HOME and PIP_CACHE_DIR; HOME and CODEX_HOME remain unchanged. No GitHub token is forwarded. Only the dedicated artifacts directory is uploaded.

Driver phases:
1. Reject an unfrozen or malformed manifest, mismatched source/schema/payload/image identity, unapproved event, or carrier source edits. Journal phase start before work and completion/failure afterward.
2. Configure the clean baseline with default testing, all plugins and mavsdk_server enabled, WERROR and SUPERBUILD. This builds the pinned protoc/gRPC dependencies. Record generator binaries, package versions and the complete dependency-prefix file hashes.
3. Run tools/regenerate_all.bash --no-container with no skip flags on the baseline; require clean generation. Candidate uses the same unchanged dependency install. Run the same entire official generator twice on candidate and require byte idempotence and an explicit attributable path allowlist. Record the observed generated-file count rather than assuming 18.
4. Build the normal baseline and final generated candidate with default unit/system/server targets. Run each complete default test executable, retaining XML, counts and binary hashes. Require all17 new I/O cases in the ordinary SYSTEM runner and all four enum/wire boundary cases in the ordinary SERVER runner to execute and pass. This is separate from the focused harness.
5. Only after default baseline controls, apply the frozen test-only overlay to baseline. Compile the same owner harness against real source: baseline PROFILE_TESTS=OFF, candidate ON. Require exact runtime case/failure identities. A configure/build failure is NEVER baseline RED. The provisional expected baseline is five assertion failures and one passing oversized-payload control; candidate has17 cases.
6. Build/install the C wrapper, stage libcmavsdk in py/mavsdk/mavsdk/lib, install the official Python development requirements/editable package into an isolated venv, and run the default py/test suite with the candidate native install on LD_LIBRARY_PATH.
7. Execute owner-supplied, hash-frozen frontend/UDP/gRPC probes via argument vectors. Their scratch-generated Python stubs stay outside SDK source. Verify candidate source and shared dependency prefix remain unchanged.

Manifest interface:
- schema_version1 and frozen:true are mandatory; unknown/missing root keys fail.
- patches have role, path, sha256 and exact paths. Roles: native, api-tests, proto, baseline-tests. Exactly one native/proto/baseline-tests input is required; API tests may add another separate patch.
- candidate_expected_files must bind EVERY SDK patch path after overlay; proto_expected_files binds FTP schema; baseline_expected_files binds the test-only baseline overlay.
- harness_files use path/sha256/destination. The example pins the owner's CMakeLists but still needs the final source/runtime freeze.
- baseline_harness_cases/failures and candidate_harness_cases are exact full names. These are functional runtime expectations, not absent-new-API compilation expectations.
- regeneration_allowlist is deliberately reviewable. The example lists18 existing FTP artifact paths from the real repository; actual output is measured rather than forced to that count. Unrelated generator changes fail.
- extra_python_requirements, when needed, is one hash-frozen requirements file.
- probe_support_files are hash-validated adjacent support modules; they are not executed directly.
- probes each use name/path/sha256/argv/required. Allowed placeholders: baseline_sdk, candidate_sdk, candidate_install, deps, candidate_build, output_json, scratch. No shell interpolation is used. The frozen manifest requires frontend-contract and python-ftp-transport as required probes; grpc-ftp-transport is also required for the hosted actual gRPC boundary. The final API-owner probe hashes and CLI are bound in manifest.json; the obsolete asyncio-enum AST probe is excluded.
- The example now includes the final native-only fb41ae43 patch and remains intentionally non-executable (frozen:false) while the API/test/probe payload is unfinished. Baseline overlay hashes now come from root-native-payload-assembly.json. The API-test v2 patch and its source hashes are now included. Final API probe payload is now bound. If root combines production patches, its combined SHA/path list must replace the separate native/API patch records without changing the expected source file map. Root may combine native and API-test changes into one manifest-bound native patch; schema remains separate.

Receipts and artifact boundaries:
Each command records exact argv/cwd/exit/timeout, full-output SHA256 and a bounded log beginning/tail. Stage receipts are written before executing a stage. Every successful test runtime binds the invoked binary hash and XML. Source diffs use a private temporary Git index so new files are included, without commits or mutation of the actual index. Separate SDK and proto patches are retained because a dirty submodule is not represented by a superproject patch. Upload includes only dedicated logs, receipts, manifests, frozen input patches and changed attributable source; no .git/config, credentials, entire build caches or core dumps.

Failure is not silently repaired or rerun. Baseline generation drift is an environment/control failure, not behavioral RED. A failed native build blocks runtime rather than producing a false RED. Completed individual default suites keep their receipts if another fails. Normal command failures are sealed in final-summary.json and artifact-sha256.json; forced runner termination can leave a phase marked running and must be reported as incomplete, never green.

The full driver has not been executed here. Only draft syntax/static validation is appropriate before root reviews the helper. No platform-matrix, hardware/SITL, performance, or older-high-level-client compatibility claim is made by this single-container plan.


Resume review corrections:
- Fixed the image-pull PULL_RC environment assignment before Python reads it.
- Baseline checkout now has full history/tags and reads upstream version tags, matching upstream git-describe requirements.
- Verified and recorded that intended build/venv/Python staging/egg-info/cache paths are ignored by the actual source rules, so source snapshots exclude them without weakening source comparisons.
- Native expected file hashes remain a PRE-generation check; the post-style/generated snapshot governs later build/runtime immutability.
- New untracked native/test C++ files receive an explicit clang-format21 dry-run because the official formatter enumerates only git-tracked files.
- Python worker count is capped and checked (PYTHON_CPU_COUNT=2), direct style uses cores2, generator/CMake budgets stay2, and CPU affinity/quota stay2.
- Root requested timeout180 for first-time full dependency and paired native builds; no gate was removed.
- Artifact upload now selects explicit receipt/log/patch/harness/source categories, excluding whole work/build/cache trees.
- Root assembly supplied the exact baseline test-overlay hash map.

Additional independent-review corrections:
- Default GTest exit1 is captured and its XML/binary/runtime inventory saved before the phase reports failure. This preserves assertion attribution and permits later independent diagnostic gates; unexpected failures still make the whole helper fail.
- Explicit required-case receipts prove all17 ordinary SYSTEM regressions and all four ordinary SERVER regressions actually executed and passed, excluding skipped/disabled/failed cases.
- All three frozen owner probes are required: source contract, actual Python/UDP transport and actual gRPC/UDP transport. Python stub generation remains scratch-only.
- Both official generation venv package inventories are recorded after generation; this is provenance, not a claim that upstream pip requirements form a locked environment.

Ready-to-package input manifest:
manifest.json is the hash-frozen input declaration, including all three required owner probes and their adjacent support module/requirements. manifest.example.json preserves the same interface with frozen:false. Frozen inputs do not assert hosted validation has passed and do not authorize publication or dispatch. The driver/workflow have not run. Resume mechanical checks additionally prove that an ordinary GTest assertion failure leaves its runtime inventory and still fails the gate, required-case failure cannot pass, and only the exact expected baseline assertion inventory earns behavioral RED.

The bounded subprocess loop also enforces its timeout if a process closes stdout before exiting, or a descendant retains the output pipe.
