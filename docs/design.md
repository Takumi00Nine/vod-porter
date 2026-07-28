# vodp 完成版 設計書 v1.2（2026-07-27・opus-system-designer 作成・Codex 2巡42件全採用・リーダー承認済み）

要件の正本: 同ディレクトリ `requirements.md`（v1.1＋本設計での差し戻し2件を承認済み＝★2/★6）。

## リーダー裁定（★1〜★7・2026-07-27）
★1 説明文フッタ `[vodp:vod=<id>]` **採用**（最終行・目立たない形式。⚠️公開文面に入るため本人レビュー対象＝初の本番運用（#3）前に確認）／★2 **承認**＝M5 AC を「insert 結果不明窓は recovery_required で停止・手動 resolve」に修正／★3 **承認**＝リポジトリ丸ごと移動・履歴保持（物理移動はリーダーが E2E 時に実施。実装は現位置 `~/work/twitch-vods-to-youtube/` で行う）／★4 **承認**＝dev 依存 jsonschema 追加可／★5 **承認**＝profile `[limits] earliest_date` 方式（既定 profile には移行日を設定。E2E 用 profile は別途）／★6 **承認**＝description は 5000**バイト**(UTF-8) に統一・playlist ID は64バイト保守見積り／★7 J①フラグ維持。

## 1. A vs B 判断（承認済み）
- 既存コード: **B=書き直し**。app/ はテスト不能構造（Config クラス変数のロード時読込・print・None返し・平坦import）で、台帳/deadline/SIGTERM/JSON契約/終了コードは横断的関心事＝後付け不能。移植は知識のみ（duration表記・yt-dlp opts・OAuth scopes 3種・.env キー名・12h/256GB）。旧 app/,sh/ は legacy/ へ git mv して1リリース凍結＋tag v0-legacy（後戻り手段）。
- CLI: **A=argparse(stdlib)**。追加依存0・parser.error=exit 2 が M4 と一致・subparsers ネストで `meta set` 可。M2 優先順位は argparse default 全 None→明示指定の有無を見て profile→既定で埋める Settings 層。

## 2. レイアウト（M21改）
`src/vodp/{cli/{parser,settings,main,commands/*},core/{exitcodes,errors,runctx,timerange,profile,template,preflight,jsonout,logging,reasons},infra/{twitch,youtube,auth,downloader,ffprobe,ledger,locking,signals,deadline,retry,fsops},schemas/*.json}` ＋ `profiles/`・`packaging/launchd/`・`tests/`・`legacy/`。
pyproject: `[project.scripts] vodp=vodp.cli.main:main`・requires-python>=3.11（tomllib）。VODP_HOME 優先順位 `--home` > `VODP_HOME` > パッケージ既定。src 配下に絶対パス禁止（AC: `! grep -R "/Users/" src/vodp`）・os.chdir 廃止・sh wrapper 全廃。

## 3. コマンドツリー（M1改）
`vods list|upload` / `videos list|publish|delete` / `meta set` / `playlist list|create|add|delete|remove-item` / `cache clean` / `ledger recover|repair|resolve` / `auth login|migrate` / `launchd generate` / `check` / `whoami`。全 --json・未知値 exit 2。
- 削除系5種: 既定 dry-run・実行は対話時も常に `--yes` 必須・削除前に id+title readback 出力・readback 失敗なら削除しない。--non-interactive で --yes 無しは exit 2。
- `videos list`＝channels.list(contentDetails)→uploads→playlistItems.list 全ページ（search.list 不使用。uploads は owner 認証で private/unlisted も返る＝裏取り済）。
- `meta set`＝videos.list で現行 snippet 読取→未指定項目マージ→update(part=snippet・title+categoryId 必須再送)。update は part 丸ごと置換（公式確認）。
- 終了コード 2>4>5>6>3>0。API失敗は `FetchResult(ok, items, complete)` 型で空リストと分離し必ず 5。exit 2 のみ JSON なし・他は必ず stdout に単一 JSON・人間向けは全て stderr。JSON Schema はコマンド別 `schemas/<command>.json`（run/summary は共通 defs・videos list は別 item 型）。

## 4. 台帳 詳細設計（リスク中核・exactly-once 優先で fail-closed）
- **source of truth**: YouTube 実状態が正本・台帳は予約票＋進捗キャッシュ・logs/JSON は派生物。「どの VOD に insert を試みたか」の正本は台帳のみ→橋渡しに説明文フッタ `[vodp:vod=<id>]`（★1採用）。
- **ロック**: チャンネル単位 `<VODP_HOME>/run/channels/<channel_id>.lock` を flock(LOCK_EX|LOCK_NB) で実行全体保持＋registry に canonical ledger path を記録・別 ledger からの書込は exit 5。reserve は同一 flock 下で「末尾再読→状態再判定→追記」の check-and-append。
- **レコード3軸**: `outcome`{pending,done,terminal_skip,retryable_failure,permanent_failure} × `stage`(到達最高) × `last_error`。キー=(youtube_channel_id, twitch_vod_id)。JSONL append-only・flock→write→flush→os.fsync。破損行1行でも exit 5(ledger_corrupt)。
- **stage**: reserved→insert_inflight→uploaded→processed→meta_verified→playlist_added→completed。非直線: insert_inflight/playlist_create_inflight/playlist_add_inflight（**呼出前に** write-ahead fsync）／processing_pending／rejected／recovery_required／deleted(tombstone)。
- **exactly-once**: 非冪等 insert 3種は自動 retry 一切なし（googleapiclient num_retries=0・retry 可は冪等 GET/list と videos.update のみ・予算は単一 Budget 共有）。*_inflight 復帰時は uploads 全ページ照合し、**未発見でも自動再insertしない**＝recovery_required で停止・exit 5・`vodp ledger resolve --vod <id> --action reinsert|abandon --yes` を提示。照合が truncate/API失敗でも fail-closed。
- **再開キュー**: 新規VOD探索より先に range 非依存で未解決行（*_inflight/processing_pending/recovery_required/privacy_lock）を全件キューイング。
- **intent**: reserved に不変 intent（privacy/playlist/title/desc/tags/category 正規化値）＋intent_hash。再開時 hash 不一致は exit 5。明示コマンドは stage 軸と別の `operation` イベントとして追記（複数 playlist membership 表現可）。
- **insert 後検査**: videos.list(part=status) で uploadStatus 先行確認。processed→次段階／uploaded(処理中)→processing_pending（skipped 扱い）／rejected,failed→rejected(permanent)。privacy readback は processed 後にのみ 5回/30秒・不一致→privacy_lock→exit 6。
- **クラッシュ窓 C1〜C10**: C1 reserve前=通常／C2 DL中=.part 掃除して再DL／C3 insert 応答喪失=insert_inflight→照合→未発見でも recovery_required／C4 応答後追記前=照合→uploaded 追記／C5 readback 中=publish から／C6 update 応答喪失=readback 冪等／C7 playlistItems.insert 応答喪失=playlist_add_inflight→playlistItems.list(playlistId,videoId) で確定／C8 行の部分書込=exit 5・repair は退避＋書込停止・非末尾破損は自動修復禁止／C9 rename後insert前=成果物再利用／C10 completed 後掃除前=冪等掃除。
- **ledger repair**: 元ファイル退避＋recovery_required で書込系停止。リモート全照合が済むまで upload 不許可。

## 5. 認証状態機械（M6/S3・10状態）
A token無→4／B 破損→.bak 退避→4／C 期限切れrefresh可→refresh 継続／D refresh失敗(RefreshError)→以降継続禁止・4（暗黙ブラウザ起動なし）／E client_secret無→token valid なら継続・不足時 5／F expired かつ refresh_token 無→4／G 未期限切れだが token 欠落/不正形式→4／H scope 不足（集合比較）→4／I valid 後の 401/invalid_grant→再認証せず 4／J token 読取 PermissionError→5。
保存: O_EXCL tmp→fchmod(0600)→write→fsync→os.replace→親dir fsync。`auth migrate` は既存 token.json を絶対上書きしない(exit 5)＝冪等。`<token.json>.lock` で load→refresh/login/migrate→save 直列化。run_local_server は redirect_stdout(sys.stderr)。初期化順はコマンド別（auth/ledger 系は channels.list 不要）。

## 6. dry-run 境界（M7）
判定はあらゆる初期化より前。ロック未作成（既存には LOCK_SH 参加・無ければスナップショット読取＋警告）・logs/ 未作成（stderr のみ）・token 保存/.bak なし・台帳読取のみ・downloads mkdir なし・対話時も OAuth 開始禁止(exit 4)。許可＝読取 API とメモリ内 refresh のみ。

## 7. M22 説明文組立（順序固定）
テンプレ展開→{playlist_url} を単独行1行へ収束→保護フッタ [vodp:vod=..] を最終行付与→CRLF→LF 正規化＋行末空白除去→UTF-8 バイト検査。出力は `プレイリスト: https://...`（山括弧なし）。複数 placeholder/異なる既存 URL 行併存→exit 2。冪等。孤児防止＝作成前に ID 長64バイト見積りで長さ検証・超過は作成せず exit 2・境界近傍は新規作成拒否（--playlist-id か事前作成を要求）。`meta set` はフッタ保護（必ず再付与・別 VOD ID への変更拒否）。復元照合は playlistItems 列挙→videos.list(part=snippet)。

## 8. その他コンポーネント
- Twitch(M13/M14): app token 実行内キャッシュ・401 は1回のみ再取得・users?login 1回・videos?user_id&type=archive&sort=time&first=100&after で range.start 到達まで・到達前に --max-pages 打切りは exit 5。duration は re.fullmatch・全群 None は duration_unknown で failed。
- YouTube(M10〜M12): insert=part="snippet,status" 完全 body・private 固定。publish=事前 status 読取→allowlist(privacyStatus/embeddable/license/publicStatsViewable/publishAt/selfDeclaredMadeForKids) マージ→update(part=status)。playlist=playlists.list(mine,50) 全ページ完全一致→0件作成・2件以上 exit 5・追加前に playlistItems.list(playlistId,videoId) 重複判定。
- preflight(M18): reserve 前＋再開時も DL/insert 前に再実行。title≤100文字・description≤5000バイト・tags≤500文字・`<` `>` 禁止・description-file 存在・未知テンプレ変数。空き容量=尺×0.75MB/s×1.2。ffprobe 不在 exit 5。
- 無人耐性(M15): 残 deadline を requests timeout/googleapiclient transport/ffprobe subprocess/yt-dlp progress_hook/OAuth 待受へ伝播・upload は chunk 境界で中断判定。--force は再計測のみ（12h/256GB は迂回不可）。シグナル: 1回目=フラグ・2回目=I/O 中断して finally 経路（台帳確定＋単一 JSON 出力を1回だけ実施し exit 5）・3回目=os._exit(5)。
- ログ(M19): logs/YYYY-MM-DD.log 1行1JSON(JST/run_id)・シークレットは formatter 層マスク（sentinel テスト）・hard cap 5MiB（到達マーカー1行で以後停止）・保持30日・yt-dlp は quiet+noprogress+logger 差替。
- DL(M17): outtmpl `vodp-<vod_id>.%(ext)s`・確定パスは progress_hook('finished') 正・info['requested_downloads'][0]['filepath'] 副で実在検証・同一FS tmp→os.replace。

## 9. 実装分割（1ワーカー直列 S1→S8）
- S1 骨格: pyproject/cli parser+settings/exitcodes/errors/runctx/reasons/jsonout/schemas/全コマンド stub。AC=M1,M2,M4(exit2) parser テスト緑
- S2 core: timerange/profile/template/preflight/profiles/example.toml。AC=M3,M9,M18＋固有文字列 grep 0件＋`! grep -R "/Users/" src/vodp`
- S3 infra基盤: locking/ledger/signals/deadline/retry/fsops/logging。AC=M5,M16,M19＋§4 全遷移の単体AC
- S4 twitch+vods list。AC=M13,M14
- S5 auth+youtube+auth/whoami/playlist/videos/meta コマンド。AC=M6(10状態),M10〜M12,videos list ページネーション,meta set マージ保持
- S6 downloader/ffprobe+upload/publish/check+台帳結線。AC=M4,M7,M8,M15,M17,M22（M15/M17 の統合AC はここ）
- S7 launchd generate+README+移行手順+legacy 移動。AC=M21
- S8 テスト総仕上げ。AC=M20
S1 の stub は後続 S が編集する前提（単一ワーカー直列）。

## 10. テスト戦略（M20）
conftest で socket.socket.connect を autouse 差替＝実ネット完全遮断。差替点: requests.Session.request(FakeTransport)／vodp.infra.youtube.build→FakeYouTube(呼出列記録)／yt_dlp.YoutubeDL→FakeYDL(tmp_path 実ファイル生成)／ffprobe subprocess.run。fcntl.flock と os.fsync は実物（同一プロセス2 fd で二重取得失敗検証）。deadline は time.monotonic monkeypatch。M8 は jsonschema(dev)。M21 は tmp cwd から subprocess で `python -m vodp --help`＋AC-3b改。実 API・実 OAuth・launchd 実起動の結合検証はリーダー担当。

## 11. 移行計画
0 tag v0-legacy 凍結／1 `~/work/tools/twitch-vods-to-youtube/` へ丸ごと移動（履歴/remote 維持・**リーダーが E2E 時に実施**）・app/,sh/→legacy/ git mv（実装ワーカー）／2 venv は移動で壊れる→新パスで `.venv` 再作成＋`pip install -e .`／3 秘密は `<VODP_HOME>/.env` のみ（TWITCH_CLIENT_ID/SECRET・実環境変数優先）・固有設定は profiles/*.toml（未指定時 default.toml・無ければ VOD 系 exit 5）／4 client_secret.json→`auth/`／5 `vodp auth migrate` で pickle→JSON(0600)・旧 .migrated 保持・pickle 読取コードは1か月後削除（README 期限）／6 既存アップ済み VOD の二重投稿対策＝profile `[limits] earliest_date`=移行日／7 launchd は vodp 直接実行・`vodp launchd generate` が絶対 .venv/bin/vodp・WorkingDirectory・ログ親dir mkdir・ThrottleInterval・KeepAlive なし・StartCalendarInterval・EnvironmentVariables(VODP_HOME/PATH) を生成／8 1週間 --dry-run 並走→切替→1か月後 legacy/ 削除。
