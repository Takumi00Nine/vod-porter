**English** | [日本語](#日本語)

# vodp -- Stream Archive Transport/Management CLI (currently Twitch → YouTube)

![Python >=3.11](https://img.shields.io/badge/python-%3E%3D3.11-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)
![Tests 399 passing](https://img.shields.io/badge/tests-399%20passing-brightgreen)

## Overview

Subcommand CLI (`vodp`, package `vod-porter`) that discovers stream
archives, downloads them, uploads them privately to a destination
platform, verifies the readback, adds them to a playlist, and records
every step in a crash-safe ledger so nothing is ever uploaded twice.
Currently supports Twitch → YouTube; the engine/profile split keeps the
door open to other source/destination pairs later. Replaces the old
`app/`+`sh/` scripts, which have been deleted (2026-07-28, by request --
they were a stopgap, not worth keeping around). The tag `v0-legacy` is
the only rollback path: `git checkout v0-legacy` restores the last
commit before this rewrite. See `docs/design.md` for the full design and
`docs/requirements.md` for the requirements this satisfies.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`vodp` needs Python ≥3.11 and `ffprobe` on `PATH` (from ffmpeg). Moving
this repo to a new path? The venv itself does **not** survive a move
(paths are baked into `pyvenv.cfg`/shebangs) -- delete `.venv` and re-run
the two commands above at the new location.

## Configuration

vodp's state/config directory (`VODP_HOME`) resolves in this order:
`--home` > `$VODP_HOME` env var > `~/.vodp` (package default).

```
<VODP_HOME>/
  .env              # secrets (see below) -- gitignored, 0600 recommended
  auth/             # token.json (0600) + client_secret.json
  profiles/         # optional per-install overrides of src/vodp/profiles/*.toml
  ledger/           # <channel_id>.jsonl + registry.json (state, not code)
  run/channels/      # flock files (multi-run guard)
  logs/             # logs/YYYY-MM-DD.log, 1 line = 1 JSON, JST
  downloads/        # transient download workspace
```

`ledger/registry.json` maps each channel id to its canonical ledger path,
stored **relative to `VODP_HOME`** whenever that path lives under it (the
default `<channel_id>.jsonl` location always does) -- so relocating the
whole `VODP_HOME` directory (rename/move/copy) keeps every canonical
ledger path resolving correctly, with no manual repair. A registry
written by an older vodp version (absolute paths even for entries under
home) is read correctly either way and opportunistically rewritten to
the relative form the next time it's touched; only a `--ledger` override
path that's genuinely outside `VODP_HOME` stays absolute (correctly --
it's not affected by a home move either way).

### Secrets: `<VODP_HOME>/.env`

```env
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL_LOGIN=your_channel_login
```

Real environment variables always override the file (useful for launchd's
`EnvironmentVariables` block without editing `.env`).

### YouTube OAuth

1. Download an OAuth "Desktop app" client from Google Cloud Console as
   `<VODP_HOME>/auth/client_secret.json`.
2. Run `vodp auth login` once, interactively, to mint `<VODP_HOME>/auth/token.json`.
3. Already have a `token.pickle` from the old scripts? Run
   `vodp auth migrate --pickle-path /path/to/token.pickle` instead of a
   fresh login (refuses to overwrite an existing `token.json` -- it's
   idempotent, not "latest wins").

### Profiles: channel-specific values

Everything channel-specific (title/description templates, tags,
categoryId, playlist name, the migration-day cutoff) lives in a
`<name>.toml` profile file, never as a literal value in `src/vodp`'s own
*code* (see `src/vodp/profiles/example.toml`). vodp looks for
`<name>.toml` in `<VODP_HOME>/profiles/` first (your override), then
falls back to the default profiles shipped *inside* the `vodp` package
itself (`src/vodp/profiles/*.toml`, installed as package data -- R6,
Codex/tester review round 2: these used to live at the repo root, outside
the installed package, so a real non-editable `pip install .` could never
find its own default profile; a plain `pip install -e .` happened to
paper over it by pointing straight back at the checkout). `--profile
default` is used unless `--profile <name>` is given; a missing profile is
exit 5, not exit 2 (it's a setup problem, not a bad argument).

`[limits] earliest_date` guards against re-uploading anything the old
scripts already published before you switched to `vodp` -- set it to your
migration date. Applied identically by `--dry-run` and the real run
(team-lead E2E follow-up: dry-run used to skip this cutoff entirely, so
it could "promise" a VOD the real run then silently dropped, violating
M7's "dry-run is a complete preview" guarantee) -- a VOD excluded by it
is reported in both as `status: skipped, reason_code: out_of_range`
(distinct from `dry_run`/no-op-because-already-uploaded etc.), and never
counts as a found target for the `--allow-empty`/exit-3 decision.

`[channel] expected_channel_id` (F5, Codex/tester review round 2,
differed-items follow-up; requirements.md 推奨S7) is optional but
recommended: every write-type command (`vods upload`, `videos publish`/
`delete`, `meta set`, `playlist create`/`add`/`update`/`delete`/`remove-item`,
`channel update`)
confirms the currently authenticated account's channel id matches it
*before* doing anything -- a wrong OAuth account (e.g. authorized
against a personal channel by mistake) fails fast (exit 5) instead of
silently uploading/publishing/deleting on the wrong channel with no one
watching an unattended run. Leaving it unset (the packaged default)
skips this check entirely -- it's opt-in, not a new default requirement.

## Migrating from the old app/+sh/ scripts

The old scripts are gone (not archived) -- this is the whole tool now.
Switching over is: `vodp auth migrate` (or a fresh `vodp auth login`),
set up `<VODP_HOME>/.env` and a profile with `[limits] earliest_date` set
to today, hand it to the lead for a real end-to-end run, then point
launchd at `vodp` instead of `sh/run_upload.sh` -- no parallel-run
period, no scheduled deletion later. If you ever need to see the old
code, `git checkout v0-legacy` is the only way; it is not present in this
working tree.

## Commands

```
vodp vods list --range 2026-07-27 2026-07-27
vodp vods upload [--range START END] [--dry-run] [--max-uploads N] [--allow-empty]
                 [--playlist NAME | --playlist-id ID] [--deadline SECONDS]
vodp videos list | publish --video-id ID --privacy public | delete --video-id ID [--yes]
vodp videos status --video-id ID [ID2 ...]
vodp meta set --video-id ID [--title ...] [--description-file ...] [--tags a,b] [--category-id 22]
                            [--playlist-id ID]
vodp playlist list | create --name NAME | add --name NAME --video-id ID
                    | update --name NAME [--new-title T] [--description D]
                    | delete --playlist-id ID [--yes] | remove-item --playlist-id ID --video-id ID [--yes]
vodp channel show | update --description DESCRIPTION [--dry-run]
vodp cache clean [--yes] [--max-age-hours 24] [--channel ID]
vodp ledger recover --channel ID | repair --channel ID | resolve --channel ID --vod ID --action reinsert|abandon --yes
vodp auth login | migrate --pickle-path PATH
vodp launchd generate --venv-python ABS_PATH --working-directory ABS_PATH [--out PATH]
vodp check [--range START END]
vodp whoami
```

Every subcommand accepts `--profile`, `--home`, `--non-interactive`,
`--json` (accepted for compatibility; JSON is always on stdout, see
below), and `--ledger` (ledger path override). `--range` accepts either a
date (`2026-07-27`, expands to the full JST day) or a full datetime
(`"2026-07-27 09:00:00"`). The lists above show the commands, not every
flag (e.g. `--dry-run`, `playlist create --privacy`,
`launchd generate --label`) -- run `vodp <command> <subcommand> --help`
for the full option list. `meta set` only ever merge-updates
`part=snippet` (title/description/tags/categoryId) -- it does *not*
accept `--playlist` (only `--playlist-id`, see the M22 section below) and
explicitly refuses `--privacy` (exit 2, R9, Codex/tester review round 2)
rather than silently accepting and ignoring it; use
`vodp videos publish --video-id ID --privacy ...` for a privacy change
(it also does the readback verification M11 requires).

`playlist update --name CURRENT_TITLE` requires at least one of
`--new-title`/`--description` (exit 2 if both are omitted; giving both
together is fine -- they're independent, not mutually exclusive) and
resolves the target
via the same `find_playlist_by_name` lookup `playlist add` uses -- but,
unlike `add`, never auto-creates: a name that doesn't resolve to exactly
one playlist is refused (exit 5), the same posture as the misdeletion
guard below. Because `playlists.update` replaces the whole `snippet` part
(not just the given fields), it always reads the current snippet back via
`playlists.list` first and merges in only the touched field(s) -- title is
always re-sent even for a description-only change, since it's a required
API field. `status` is never touched. `--dry-run` makes no API calls at
all (same posture as `create`/`add`'s dry-run, not `delete`'s
read-then-skip).

`channel show` is a read-only detail view of the authenticated channel
(id/title/description/customUrl, `part=snippet,brandingSettings`) --
`whoami` stays the cheap `part=snippet`-only identity check it always
was, unchanged. `channel update --description DESCRIPTION` only ever
writes `part=brandingSettings` (`channels.update` doesn't support
`part=snippet` at all -- `snippet.title`/`description` are read-only to
API consumers). It reads the current `brandingSettings` back via
`channels.list` first and merges in only `description`, preserving every
other field (`keywords`, any legacy sub-objects, etc.) untouched --
including always re-sending the channel's *current, unchanged* `title`,
since `channels.update` rejects any actual title change outright
(`channelTitleUpdateForbidden`; renaming a channel is only possible in
YouTube Studio, not through this API at all) -- there is accordingly no
`--title` flag here. `--dry-run` makes no API calls at all (same posture
as `playlist create`/`add`/`update`'s dry-run).

`videos status --video-id ID [ID2 ...]` is a read-only diagnostic: it
calls `videos.list` with `part=status,processingDetails` (one call per up
to 50 ids) and shows each video's `uploadStatus`/`failureReason`/
`rejectionReason`/`privacyStatus` plus `processingStatus`/
`processingProgress` (parts total/processed, estimated time left)/
`processingFailureReason` -- useful for checking how far a long upload's
YouTube-side transcoding has gotten. It never calls a write-type API. A
requested id YouTube doesn't return is reported per-item as
`status: "failed"`/`reason_code: "not_found"` (exit 6, `PARTIAL_FAILURE`)
rather than aborting the whole batch -- the other ids are still reported.

Destructive commands (`videos delete`, `playlist delete`,
`playlist remove-item`, `cache clean`) default to a **dry-run preview** and
require `--yes` to actually execute; under `--non-interactive`, omitting
`--yes` is an argument error (exit 2) instead of a silent no-op, so an
unattended job never mistakes a preview for a completed action. Every one
of them reads the target back (id+title, or ledger state for `cache
clean`) *before* deciding anything, and refuses to delete -- in dry-run
*and* with `--yes` alike -- if that readback can't confirm the target
really exists (an empty `videos.list`/`playlists.list` result, a
`playlistItems.list` API failure, or a ledger that's currently
`ledger_corrupt`); see `tests/test_cli_youtube_commands.py` and
`tests/test_cli_check_cache.py` for the misdeletion-prevention AC per
command (Codex/tester review round 2, R5).

## JSON contract & exit codes

Every invocation prints exactly one JSON document to stdout:
`{"run": {...}, "videos": [...], "summary": {...}}`, **except exit 2**,
which never emits JSON at all -- only stderr text (usage from argparse
itself, or a human message from a post-parse validation error like
`--range` start > end). Exit 2 is "bad CLI argument", full stop,
regardless of whether argparse or a handler caught it; every other exit
code always includes the JSON envelope. Human-readable progress/errors
otherwise go to stderr only.

| Exit | Meaning |
|---|---|
| 0 | success, or all videos normally skipped |
| 2 | bad CLI argument |
| 3 | zero target videos (pass `--allow-empty` on `vods upload` to make this 0) |
| 4 | interactive auth required (`--non-interactive` forbade it) -- run `vodp auth login` |
| 5 | environment/API failure, or a ledger `recovery_required` row is blocking the channel |
| 6 | one or more videos failed (e.g. `privacy_lock`) |

Priority when several apply at once: `2 > 4 > 5 > 6 > 3 > 0`.

## Unattended operation (launchd)

```bash
vodp launchd generate \
  --venv-python /abs/path/to/.venv/bin/vodp \
  --working-directory /abs/path/to/repo \
  --out ~/Library/LaunchAgents/com.vodp.vods-upload.plist
launchctl load ~/Library/LaunchAgents/com.vodp.vods-upload.plist
```

No shell wrapper: `vodp` is invoked directly by launchd (the old
`sh/run_upload.sh` piped through `| tee`, which swallowed the real exit
code -- see `docs/requirements.md` G18). The generated plist has no
`KeepAlive` (a failed unattended run should not be relaunched immediately)
and sets `VODP_HOME`/`PATH` explicitly.

## Recovering from a lost `insert` response (ledger `recovery_required`)

YouTube's `videos.insert` is not idempotent, so vodp never retries or
auto-repeats it. If the network drops *after* the upload reached YouTube
but *before* vodp recorded the response, the ledger row is left at
`insert_inflight`. On the next run, vodp automatically searches your
uploads for the hidden footer marker (`[vodp:vod=<twitch_vod_id>]`,
appended to every description vodp writes) before doing anything else:

- **found** -- the row resumes forward (publish/playlist/etc.) with no
  new insert call.
- **not found** -- the row moves to `recovery_required` and the whole run
  exits 5. Resolve it manually:

  ```bash
  # confirm by hand: did the upload actually happen? Check YouTube Studio
  # for a private video titled like the VOD, or search the description
  # for the footer marker above.
  vodp ledger recover --channel <channel_id>       # lists every unresolved row
  vodp ledger resolve --channel <channel_id> --vod <twitch_vod_id> --action reinsert --yes   # confirmed NOT uploaded -> retry from scratch
  vodp ledger resolve --channel <channel_id> --vod <twitch_vod_id> --action abandon  --yes   # confirmed uploaded (or give up) -> stop tracking it
  ```

  `reinsert` is refused if the row already carries a `youtube_video_id` or
  reached `recovery_required` from a post-insert stage (uploaded,
  processing, playlist, ...) -- in that case only `abandon` is offered,
  because the video is confirmed to exist and re-inserting would create a
  duplicate.

A corrupt ledger (e.g. a torn write mid-crash) blocks all writes until
`vodp ledger repair --channel <channel_id>` runs: it quarantines the
original file and auto-drops the last line only if the file's tail is the
one unambiguous signature of a torn write (missing trailing newline);
anything else stays blocked for manual inspection.

## Playlist URL in the description (M22)

`--playlist NAME` resolves (or creates) the playlist exactly once per
run, *before* any video's insert, so its URL is available to bake into
every video's description via `{playlist_url}` -- or, if the template
doesn't reference it, vodp appends `プレイリスト: <url>` as the final line
automatically. Re-running (or `meta set` touching the description) is
idempotent: the line converges instead of duplicating, and a description
that already has a *different* playlist line is a hard conflict (exit 2),
never silently overwritten. Creating a brand new playlist is refused
(exit 2) if the description is already too close to the 5000-byte limit
to safely absorb an unknown-length playlist ID -- pass `--playlist-id` for
an existing playlist, or create it manually first, in that case.
`--dry-run` only ever calls `playlists.list` (never creates), reporting
`playlist_url` if found or `planned_playlist_title` if it would need
creating.

## Unattended resilience (M15)

`--deadline SECONDS` (default 3600) bounds the whole run's wall-clock
budget; `SIGTERM`/`SIGINT` are handled gracefully (1st signal stops
taking on new work, 2nd aborts in-flight I/O, 3rd force-exits) with
everything already durably recorded in the ledger up to that point.
Idempotent reads (`videos.list`/`playlists.list`/`playlistItems.list`/
`channels.list`/`videos.update`) share one finite retry-with-backoff
budget for the whole run; the three non-idempotent writes
(`videos.insert`/`playlists.insert`/`playlistItems.insert`) are never
retried (`num_retries=0`), by construction -- see
`src/vodp/infra/retry.py`. The deadline is checked between VODs, at the
ffprobe step (bounds its subprocess timeout), before each resumable-
upload chunk, and on every yt-dlp `progress_hooks` callback during the
download itself (F6, Codex/tester review round 2, differed-items
follow-up) -- yt-dlp's own `socket_timeout` option is also set from the
remaining deadline, so a stalled connection can't hang forever even
between progress callbacks. Full internal propagation into yt-dlp's
download loop isn't practical, but these two together are enough to
guarantee it never hangs past the deadline. The one remaining
genuinely out-of-scope gap is an **interactive OAuth wait** (`vodp auth
login`'s browser flow) -- non-interactive/dry-run runs never open one at
all (M6/M7), so this only affects an operator running `auth login` by
hand.

## Known limitations (time-boxed scope, flagged for follow-up)

- Real API/OAuth end-to-end verification, and the actual repo move to
  `~/work/tools/twitch-vods-to-youtube/`, are the lead's responsibility
  (docs/design.md §11) -- everything in this repo is tested fully offline
  (`pytest -q`, socket access blocked by `tests/conftest.py`).
- A stuck playlist-creation write-ahead row (`__playlist__:<name>`,
  `playlist_create_inflight`/`recovery_required`) is only reconciled when
  a *future* run has candidate VODs **and** requests that same playlist
  again; it isn't picked up range-independently the way a video-level
  `recovery_required` row is. `vodp ledger recover --channel ID` still
  lists it once it's stuck, and `vodp ledger resolve` still works on it.
- `advance()`'s `expected_stage` guard accepts the row's *current* stage
  as a match rather than enumerating the specific allowed prior stage(s)
  for every transition; it catches stale/out-of-order writes but not
  every theoretically-invalid one.

## Development

```bash
./.venv/bin/python -m pytest -q
```

All tests are fully offline (Twitch/YouTube/yt-dlp/ffprobe are faked --
see `tests/fakes.py` and `docs/design.md` §10); `tests/conftest.py` blocks
real socket connections as a backstop. The one exception is
`tests/test_packaging.py` (M21/R6): it does a real, non-editable
`pip install --no-build-isolation --no-deps` of this repo into a
throwaway `--prefix`, using *this* interpreter's own setuptools/wheel
(dev dependency) rather than PyPI, so it stays offline too -- this is
what confirms `src/vodp/profiles/*.toml` actually ships as installed
package data instead of only working by accident via an editable
install's back-reference to the repo checkout.

---

## 日本語

# vodp -- 配信アーカイブの運搬・管理 CLI（現在は Twitch → YouTube 対応）

## 概要

サブコマンド型 CLI（コマンド名 `vodp`、パッケージ名 `vod-porter`）。配信アーカイブを検出し、ダウンロードし、非公開状態で配信先プラットフォームへアップロードし、公開範囲を読み戻して検証し、プレイリストへ追加し、すべての段階をクラッシュ耐性のある台帳に記録することで二重投稿を防ぐ。現状は Twitch → YouTube に対応しているが、エンジンとプロファイルを分離した設計になっており、将来別の配信元/配信先の組み合わせにも拡張可能。旧 `app/`+`sh/` スクリプトを置き換えるもので、それらはすでに削除済み（2026-07-28・本人指示・つなぎのスクリプトで残す価値なしと判断）。`v0-legacy` タグが唯一のロールバック経路であり、`git checkout v0-legacy` でこの書き直し前の最終コミットへ戻せる。全体設計は `docs/design.md`、それが満たす要件は `docs/requirements.md` を参照。

## インストール

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`vodp` の実行には Python ≥3.11 と、PATH 上の `ffprobe`（ffmpeg 同梱）が必要。本リポジトリを別パスへ移動する場合、venv 自体は移動に耐えない（`pyvenv.cfg`/shebang に絶対パスが焼き込まれるため）。`.venv` を削除し、新しい場所で上記2コマンドを再実行すること。

## 設定

vodp の状態/設定ディレクトリ（`VODP_HOME`）は次の優先順位で解決される：
`--home` > `$VODP_HOME` 環境変数 > `~/.vodp`（パッケージ既定）。

```
<VODP_HOME>/
  .env              # 秘密情報（下記参照）-- gitignore 対象・0600 推奨
  auth/             # token.json (0600) + client_secret.json
  profiles/         # src/vodp/profiles/*.toml のインストール別上書き（任意）
  ledger/           # <channel_id>.jsonl + registry.json（状態であってコードではない）
  run/channels/      # flock ファイル（多重起動防止）
  logs/             # logs/YYYY-MM-DD.log、1行1JSON、JST
  downloads/        # ダウンロード用の一時作業領域
```

`ledger/registry.json` は各チャンネルIDと正規台帳パスの対応を保持するが、そのパスが `VODP_HOME` 配下にある限り（既定の `<channel_id>.jsonl` は常にそう）**`VODP_HOME` からの相対パス**として保存する -- これにより `VODP_HOME` ディレクトリ自体をまるごと移設（rename/move/copy）しても、正規台帳パスの照合が壊れず、手動修正が不要になる。旧バージョンの vodp が書いた registry（`VODP_HOME` 配下のエントリでも絶対パスで保存されている）はどちらの形式でも正しく読み込め、次にそのエントリに触れた際に相対形式へ書き戻される（機会主義的マイグレーション）。`--ledger` で `VODP_HOME` の外を指す上書きパスのみ絶対パスのまま保持される（正しい挙動 -- `VODP_HOME` の移設で影響を受けるべきではないため）。

### 秘密情報: `<VODP_HOME>/.env`

```env
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL_LOGIN=your_channel_login
```

実環境変数は常にファイルより優先される（`.env` を編集せずに launchd の `EnvironmentVariables` ブロックで上書きする用途に便利）。

### YouTube OAuth

1. Google Cloud Console から OAuth「デスクトップアプリ」クライアントをダウンロードし、`<VODP_HOME>/auth/client_secret.json` として保存する。
2. `vodp auth login` を一度だけ対話的に実行し、`<VODP_HOME>/auth/token.json` を発行する。
3. 旧スクリプトの `token.pickle` が既にある場合は、新規ログインの代わりに `vodp auth migrate --pickle-path /path/to/token.pickle` を実行する（既存の `token.json` は上書きせず拒否する＝冪等であり「後勝ち」ではない）。

### プロファイル: チャンネル固有の値

チャンネル固有の値（タイトル/説明テンプレート・タグ・categoryId・プレイリスト名・移行日カットオフ）はすべて `<name>.toml` プロファイルファイルに置き、`src/vodp` 自身のコードにリテラル値として書かない（`src/vodp/profiles/example.toml` を参照）。vodp はまず `<VODP_HOME>/profiles/` 内の `<name>.toml`（個別上書き）を探し、見つからなければ `vodp` パッケージ自体に同梱された既定プロファイル（`src/vodp/profiles/*.toml`、パッケージデータとしてインストールされる -- R6・Codex/tester レビュー第2ラウンド：以前はリポジトリのルートに置かれておりインストール済みパッケージの外側だったため、非editableな実 `pip install .` では自分自身の既定プロファイルを見つけられなかった。`pip install -e .` はチェックアウト先を直接参照するため、たまたま問題を覆い隠していた）にフォールバックする。`--profile <name>` を指定しない限り `--profile default` が使われる。プロファイルが見つからない場合は exit 2 ではなく exit 5（引数不正ではなくセットアップの問題のため）。

`[limits] earliest_date` は、`vodp` へ切り替える前に旧スクリプトが既に公開したものを再アップロードしないためのガードであり、自分の移行日を設定する。`--dry-run` と実実行のどちらでも同一に適用される（team-lead E2E フォローアップ：以前は dry-run がこのカットオフを完全にスキップしており、dry-run が「約束」した VOD を実実行が黙って除外してしまうことがあり、M7 の「dry-run は完全なプレビュー」という保証に違反していた）-- このカットオフで除外された VOD は両方とも `status: skipped, reason_code: out_of_range`（`dry_run`／既にアップロード済みによる no-op 等とは区別される）として報告され、`--allow-empty`/exit-3 判定における「見つかった対象」としてはカウントされない。

`[channel] expected_channel_id`（F5・Codex/tester レビュー第2ラウンド、差分項目のフォローアップ；requirements.md 推奨S7）は任意だが推奨設定：書込系コマンド（`vods upload`・`videos publish`/`delete`・`meta set`・`playlist create`/`add`/`update`/`delete`/`remove-item`・`channel update`）はすべて、実行前に現在認証中のアカウントのチャンネルIDがこの値と一致することを確認する -- 誤った OAuth アカウント（例：個人チャンネルで誤って認可）は、無人実行を誰も見ていない状況で誤ったチャンネルへ黙ってアップロード/公開/削除する代わりに、即座に失敗する（exit 5）。未設定（パッケージ既定）の場合はこのチェック自体をスキップする -- 新たな既定要件ではなく opt-in。

## 旧 app/+sh/ スクリプトからの移行

旧スクリプトはアーカイブではなく削除済み -- これが唯一のツールとなる。切り替え手順は：`vodp auth migrate`（または新規に `vodp auth login`）を実行し、`<VODP_HOME>/.env` と `[limits] earliest_date` を今日の日付に設定したプロファイルを用意し、実際の end-to-end 実行をリーダーに委ねてから、launchd の実行対象を `sh/run_upload.sh` から `vodp` へ切り替える -- 並走期間なし、後日の削除スケジュールもなし。旧コードを確認する必要がある場合は `git checkout v0-legacy` が唯一の方法であり、この作業ツリーには存在しない。

## コマンド

```
vodp vods list --range 2026-07-27 2026-07-27
vodp vods upload [--range START END] [--dry-run] [--max-uploads N] [--allow-empty]
                 [--playlist NAME | --playlist-id ID] [--deadline SECONDS]
vodp videos list | publish --video-id ID --privacy public | delete --video-id ID [--yes]
vodp videos status --video-id ID [ID2 ...]
vodp meta set --video-id ID [--title ...] [--description-file ...] [--tags a,b] [--category-id 22]
                            [--playlist-id ID]
vodp playlist list | create --name NAME | add --name NAME --video-id ID
                    | update --name NAME [--new-title T] [--description D]
                    | delete --playlist-id ID [--yes] | remove-item --playlist-id ID --video-id ID [--yes]
vodp channel show | update --description DESCRIPTION [--dry-run]
vodp cache clean [--yes] [--max-age-hours 24] [--channel ID]
vodp ledger recover --channel ID | repair --channel ID | resolve --channel ID --vod ID --action reinsert|abandon --yes
vodp auth login | migrate --pickle-path PATH
vodp launchd generate --venv-python ABS_PATH --working-directory ABS_PATH [--out PATH]
vodp check [--range START END]
vodp whoami
```

すべてのサブコマンドは `--profile`・`--home`・`--non-interactive`・`--json`（互換性のため受け付けるが、JSON は常に stdout に出力される。下記参照）・`--ledger`（台帳パスの上書き）を受け付ける。`--range` は日付のみ（`2026-07-27`、JST の1日全体に展開）または完全な日時（`"2026-07-27 09:00:00"`）を受け付ける。上記の一覧はコマンドの一覧であり、全フラグを網羅したものではない（例: `--dry-run`・`playlist create --privacy`・`launchd generate --label`）-- 全オプションを確認するには `vodp <command> <subcommand> --help` を実行すること。`meta set` は常に `part=snippet`（title/description/tags/categoryId）のマージ更新のみを行う -- `--playlist` は受け付けず（`--playlist-id` のみ、下記 M22 節参照）、`--privacy` は明示的に拒否する（exit 2、R9、Codex/tester レビュー第2ラウンド）-- 黙って受理して無視するのではなく。公開範囲を変更する場合は `vodp videos publish --video-id ID --privacy ...` を使うこと（M11 が要求する読み戻し検証も行う）。

`playlist update --name 現在のタイトル` は `--new-title`/`--description` の少なくとも一方を要求する（両方省略は exit 2、両方同時指定も可能 -- 互いに排他ではない）。対象解決は `playlist add` と同じ `find_playlist_by_name` を流用するが、`add` と異なり自動作成はしない -- 名前が唯一のプレイリストに解決できない場合は拒否する（exit 5、下記の誤削除防止ガードと同じ姿勢）。`playlists.update` は `snippet` パート全体を置き換えてしまうため（部分更新は不可）、必ず `playlists.list` で現在の snippet を読み戻してから触れたフィールドだけをマージする -- title は API の必須フィールドなので description のみの変更でも常に再送する。`status` には一切触れない。`--dry-run` は API 呼び出しを一切行わない（`delete` の「読んでからスキップ」ではなく `create`/`add` の dry-run と同じ姿勢）。

`channel show` は認証中チャンネルの詳細な読み取り専用ビュー（id/title/description/customUrl、`part=snippet,brandingSettings`）-- `whoami` は従来通り `part=snippet` のみの軽量な身元確認のままで変更なし。`channel update --description DESCRIPTION` は常に `part=brandingSettings` のみを書き込む（`channels.update` は `part=snippet` を一切サポートしない -- `snippet.title`/`description` は API 利用者からは読み取り専用）。必ず `channels.list` で現在の `brandingSettings` を読み戻し、`description` のみをマージする -- `keywords` や旧仕様の他のサブオブジェクトなど、触れていないフィールドはすべて保持する。title についても、現在の値を変更せず常に再送する -- `channels.update` はタイトルへの実際の変更をすべて拒否するため（`channelTitleUpdateForbidden`。チャンネル名の変更は YouTube Studio でのみ可能であり、この API では一切できない）、`--title` フラグはここには存在しない。`--dry-run` は API 呼び出しを一切行わない（`playlist create`/`add`/`update` の dry-run と同じ姿勢）。

`videos status --video-id ID [ID2 ...]` は読み取り専用の診断コマンド：`videos.list` を `part=status,processingDetails` で呼び出し（最大50 ID/回）、各動画の `uploadStatus`/`failureReason`/`rejectionReason`/`privacyStatus` と `processingStatus`/`processingProgress`（処理済みパート数・残り推定時間）/`processingFailureReason` を表示する -- 長尺動画のアップロード後、YouTube 側のトランスコード進捗を確認する用途。書き込み系APIは一切呼ばない。指定した ID のうち YouTube が返さなかったものは、バッチ全体を中断せず `status: "failed"`・`reason_code: "not_found"`（exit 6・`PARTIAL_FAILURE`）として個別に報告する -- 他の ID は正常に報告され続ける。

破壊的コマンド（`videos delete`・`playlist delete`・`playlist remove-item`・`cache clean`）は既定で **dry-run プレビュー** となり、実際に実行するには `--yes` が必須。`--non-interactive` 下で `--yes` を省略すると、サイレントな no-op ではなく引数エラー（exit 2）となるため、無人ジョブがプレビューを完了扱いと誤認することはない。いずれのコマンドも何かを決定する前に対象を読み戻し（id+タイトル、`cache clean` の場合は台帳状態）、その読み戻しで対象の実在が確認できない場合（`videos.list`/`playlists.list` の結果が空、`playlistItems.list` の API 失敗、または台帳が現在 `ledger_corrupt` の場合）は、dry-run でも `--yes` 付きでも削除を拒否する。コマンドごとの誤削除防止の受入条件は `tests/test_cli_youtube_commands.py` と `tests/test_cli_check_cache.py`（Codex/tester レビュー第2ラウンド、R5）を参照。

## JSON契約と終了コード

すべての呼び出しは、stdout に厳密に1つの JSON ドキュメントを出力する：`{"run": {...}, "videos": [...], "summary": {...}}`。**exit 2 のみ例外**で JSON を一切出力せず、stderr のテキストのみとなる（argparse 自身の usage、または `--range` 開始>終了のようなパース後バリデーションエラーの人間向けメッセージ）。exit 2 は argparse とハンドラのどちらが捕捉したかに関わらず「不正な CLI 引数」を意味し、それ以外の終了コードは常に JSON エンベロープを含む。人間向けの進捗/エラーはそれ以外は stderr のみに出力される。

| 終了コード | 意味 |
|---|---|
| 0 | 成功、または全動画が正常にスキップ |
| 2 | CLI 引数不正 |
| 3 | 対象動画0件（`vods upload` に `--allow-empty` を渡すとこれが0になる） |
| 4 | 対話認証が必要（`--non-interactive` がそれを禁止した）-- `vodp auth login` を実行すること |
| 5 | 環境/API失敗、または台帳の `recovery_required` 行がチャンネルをブロックしている |
| 6 | 1件以上の動画が失敗（例: `privacy_lock`） |

複数該当する場合の優先順位: `2 > 4 > 5 > 6 > 3 > 0`。

## 無人運用（launchd）

```bash
vodp launchd generate \
  --venv-python /abs/path/to/.venv/bin/vodp \
  --working-directory /abs/path/to/repo \
  --out ~/Library/LaunchAgents/com.vodp.vods-upload.plist
launchctl load ~/Library/LaunchAgents/com.vodp.vods-upload.plist
```

シェルラッパーなし: `vodp` は launchd から直接起動される（旧 `sh/run_upload.sh` は `| tee` を経由しており、実際の終了コードを握り潰していた -- `docs/requirements.md` の G18 参照）。生成される plist に `KeepAlive` はなく（無人実行が失敗した際に即座に再起動しない）、`VODP_HOME`/`PATH` を明示的に設定する。

## `insert` 応答喪失からの復旧（台帳 `recovery_required`）

YouTube の `videos.insert` は冪等ではないため、vodp はこれを再試行・自動再実行しない。アップロードが YouTube に到達した*後*、vodp がその応答を記録する*前*にネットワークが切断された場合、台帳の行は `insert_inflight` のまま残る。次回実行時、vodp は何よりも先に自分のアップロード動画から隠しフッターマーカー（`[vodp:vod=<twitch_vod_id>]`、vodp が書くすべての説明文の末尾に付与される）を自動検索する：

- **見つかった場合** -- その行は新規 insert 呼び出しなしで先へ進む（公開/プレイリスト等）。
- **見つからない場合** -- その行は `recovery_required` へ遷移し、実行全体が exit 5 で終了する。手動で解決する：

  ```bash
  # 手動確認：アップロードは実際に行われたか？ YouTube Studio で VOD 名に似た
  # 非公開動画を確認するか、説明文で上記のフッターマーカーを検索する。
  vodp ledger recover --channel <channel_id>       # 未解決の全行を一覧表示
  vodp ledger resolve --channel <channel_id> --vod <twitch_vod_id> --action reinsert --yes   # 未アップロードと確認 -> 最初からやり直し
  vodp ledger resolve --channel <channel_id> --vod <twitch_vod_id> --action abandon  --yes   # アップロード済みと確認（または諦める）-> 追跡を停止
  ```

  行が既に `youtube_video_id` を保持しているか、insert 後の段階（uploaded・processing・playlist 等）から `recovery_required` に到達した場合、`reinsert` は拒否される -- その場合 `abandon` のみが提供される。動画の存在が確認されており、再 insert すると重複が生じるため。

破損した台帳（例：クラッシュによる書き込み途中の切断）は、`vodp ledger repair --channel <channel_id>` を実行するまですべての書き込みをブロックする：元のファイルを隔離し、ファイル末尾が書き込み途中切断の一義的な兆候（末尾改行の欠落）である場合のみ最終行を自動的に破棄する。それ以外の場合は手動調査のためブロックされたままとなる。

## 説明文へのプレイリストURL（M22）

`--playlist NAME` は実行ごとに1回だけプレイリストを解決（または作成）し、どの動画の insert よりも先に行うため、そのURLは `{playlist_url}` を通じて各動画の説明文に焼き込める -- もしくはテンプレートがそれを参照していない場合、vodp が `プレイリスト: <url>` を最終行として自動的に追記する。再実行（または説明文に触れる `meta set`）は冪等であり、行は重複ではなく収束する。既に*異なる*プレイリスト行を持つ説明文はハードコンフリクト（exit 2）となり、黙って上書きされることはない。新規プレイリストの作成は、説明文が既に5000バイト上限に近く、不明な長さのプレイリストIDを安全に収容できない場合は拒否される（exit 2）-- その場合は既存プレイリストに `--playlist-id` を渡すか、手動で先に作成すること。`--dry-run` は `playlists.list` のみを呼び出し（作成は行わない）、見つかった場合は `playlist_url`、作成が必要な場合は `planned_playlist_title` を報告する。

## 無人耐性（M15）

`--deadline SECONDS`（既定 3600）が実行全体の時間予算を制限する。`SIGTERM`/`SIGINT` は段階的に処理される（1回目のシグナルで新規作業の受付を停止、2回目で進行中のI/Oを中断、3回目で強制終了）。それまでの内容はすべて台帳に確実に記録済み。冪等な読取（`videos.list`/`playlists.list`/`playlistItems.list`/`channels.list`/`videos.update`）は実行全体で1つの有限リトライ・バックオフ予算を共有する。非冪等な3つの書込（`videos.insert`/`playlists.insert`/`playlistItems.insert`）は構造上決して再試行されない（`num_retries=0`）-- `src/vodp/infra/retry.py` を参照。デッドラインは各VOD間、ffprobe ステップ（サブプロセスのタイムアウトを制限）、各再開可能アップロードチャンクの前、そしてダウンロード中の yt-dlp `progress_hooks` コールバックのたびにチェックされる（F6、Codex/tester レビュー第2ラウンド、差分項目のフォローアップ）-- yt-dlp 自身の `socket_timeout` オプションも残りデッドラインから設定されるため、進捗コールバックの合間であっても、停止した接続が永遠にハングすることはない。yt-dlp のダウンロードループへの完全な内部伝播は現実的ではないが、この2つの組み合わせでデッドラインを超えてハングしないことを保証するには十分。唯一の真にスコープ外のギャップは**対話的 OAuth 待機**（`vodp auth login` のブラウザフロー）であり、非対話/dry-run 実行ではこれを一切開かない（M6/M7）ため、これは手動で `auth login` を実行するオペレーターにのみ影響する。

## 既知の制限（時間制約付きスコープ、フォローアップ対象）

- 実 API/OAuth の end-to-end 検証、および `~/work/tools/twitch-vods-to-youtube/` への実際のリポジトリ移動はリーダーの責任（docs/design.md §11）-- 本リポジトリ内のすべては完全にオフラインでテストされる（`pytest -q`、`tests/conftest.py` が実ソケット接続をブロック）。
- スタックしたプレイリスト作成のwrite-ahead行（`__playlist__:<name>`、`playlist_create_inflight`/`recovery_required`）は、*将来の*実行が候補VODを持ち、**かつ**同じプレイリストを再度要求した場合にのみ解消される -- 動画レベルの `recovery_required` 行のように範囲非依存で拾われるわけではない。`vodp ledger recover --channel ID` はスタックしている限りそれを一覧表示し続け、`vodp ledger resolve` もそれに対して機能し続ける。
- `advance()` の `expected_stage` ガードは、遷移ごとに許可される具体的な直前段階を列挙するのではなく、行の*現在の*段階が一致することを受け入れる方式であるため、古い/順序違反の書き込みは検知できるが、理論上不正なものすべてを検知できるわけではない。

## 開発

```bash
./.venv/bin/python -m pytest -q
```

すべてのテストは完全にオフライン（Twitch/YouTube/yt-dlp/ffprobe はフェイク化 -- `tests/fakes.py` と `docs/design.md` §10 参照）。`tests/conftest.py` が実ソケット接続を保険としてブロックする。唯一の例外は `tests/test_packaging.py`（M21/R6）: このリポジトリを、実インタプリタ自身の setuptools/wheel（dev 依存）を使って（PyPI ではなく）、実際の非editableな `pip install --no-build-isolation --no-deps` で使い捨ての `--prefix` へインストールする -- これによりオフラインのまま、`src/vodp/profiles/*.toml` が editable インストールのチェックアウトへの参照によってたまたま動いているのではなく、実際にインストール済みパッケージデータとして出荷されていることを確認する。
