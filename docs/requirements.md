# vodp 完成版 要件定義書 v1（2026-07-27・opus-requirements-analyst 作成・Codex 33件反映・リーダー承認済み）

結論: 自作継続（乗り換えなし）。要望は「引数化」より深く、サブコマンド化＋段階台帳＋deadline/SIGTERM＋多重起動防止が完成の必須条件。

## A. 前提事実（Web裏取り 2026-07-27）
- YouTube 既定割当: search.list 100回/日・videos.insert 100回/日（専用バケット）・その他合算10,000units/日。「1本1600units＝日6本」は旧情報（2025-12改定）。update/playlists.insert/playlistItems.insert=各50, list=1 → 1本の公開処理≒101units＝実質上限は insert 100本/日。
- 未審査APIプロジェクトの insert は private ロック・public化不可（本件は本番化済み・7/27 に public 読み戻し成功＝現状影響なしだが回帰検知は必要）。
- 動画上限＝256GB または12時間の小さい方。
- Twitch Helix /videos は日付範囲フィルタなし・period 不正確 → sort=time＋cursor で新しい順に取り、下限より古い created_at で打ち切り。

## B. ユースケース
UC1 翌日ルーチン(AI実行): 前日VOD→private up→メタ確定→公開→プレイリスト→JSONでURL回収
UC2 launchd無人／UC3 手動単発(--range,--dry-run)／UC4 途中失敗後の再実行（段階再開・二重投稿ゼロ）
UC5 AIのアドホック照会（VOD一覧・動画一覧・プレイリスト一覧・メタ更新・削除）＝アドホックPython根絶が完成の定義

## C. 必須要件（M）※各「AC:」が受入条件＝テストで機械検証
- M1 サブコマンド体系: `vodp vods list|upload|publish|meta set|playlist list|create|add|videos list|delete|check|whoami`。AC: 全サブコマンド --json 対応・--help 掲載・未知値 exit 2。
- M2 共通オプション: --range/--title/--description-file/--privacy{private,unlisted,public}/--playlist/--tags/--category-id/--profile/--dry-run/--json/--non-interactive/--ledger/--max-uploads/--allow-empty。AC: 既定値・正数制約・優先順位(CLI>profile>既定)のparserテスト。
- M3 --range 補完: 日付のみ→開始00:00:00/終端23:59:59.999999 JST。main と check の両方で共通 parse_range。AC: 単体テスト＋開始>終端 exit 2。
- M4 終了コード（優先順位 2>4>5>6>3>0）: 2=引数不正, 4=対話認証が必要, 5=環境不備・API/取得失敗・台帳破損, 6=1本以上failed, 3=対象0件(--allow-empty時は0), 0=成功 or 全件正常skip。「正常0件」と「取得失敗」を型で分離しAPI失敗は必ず5。AC: 全skip・全失敗・混在・dry-run 各1テスト。
- M5 段階台帳(JSONL): reserved→uploaded(video_id)→meta_verified→playlist_added→completed（詳細設計で inflight 状態を追加＝設計書§4が正本）。記録=twitch_vod_id/twitch_user_id/youtube_channel_id/youtube_video_id/profile/stage/error/ts。fcntl ロック+fsync、破損行→exit 5。AC（★2 設計差し戻しで改訂・2026-07-27 リーダー承認）: 各段階で中断→再実行がその段階から再開し insert 未呼出・重複ゼロ。**ただし insert 応答喪失の窓は例外＝exactly-once を優先し、リモート照合で未発見でも自動再insertせず recovery_required で停止（exit 5・`vodp ledger resolve` で手動解決）**。応答喪失時の照合手順を README に明記。
- M6 非対話フェイルファスト: 認証状態表（token無/破損/期限切れrefresh可/refresh失敗/client_secret無）ごとに挙動規定。--non-interactive では run_local_server 未呼出・refresh失敗後の処理継続禁止・exit 4＋再認証手順を stderr。AC: 5状態すべて検証。
- M7 --dry-run: DL/API書込/台帳書込/ファイルシステム変更を一切しない。AC: yt_dlp・youtubeクライアント・mkdir 系モック未呼出。
- M8 JSON契約: 単一JSON（run/videos[]/summary）。status∈{uploaded,skipped,failed}＋reason_code（already_uploaded/too_long/max_uploads/privacy_lock/download_failed/api_error…）。exit 2以外の全終了コードで stdout に JSON・人間向けは stderr。AC: JSON Schema 検証テスト。
- M9 メタのテンプレート化: {date}{vod_url}{title}{playlist_url} 置換。固有値は --profile(TOML) or CLI。AC: 置換テスト＋エンジンパスに固有文字列 grep 0件、profile不在 exit 5・不正キー exit 2。
- M10 insert 時に完全 snippet 設定（private固定）→ update は status のみ。プレイリストURLを説明に入れる場合はプレイリスト先行解決。AC: insert body 全含有・update part=status のみ。
- M11 公開範囲の読み戻し検証: 上限付きポーリング（5回/最大30秒）後の不一致は failed/privacy_lock＋exit 6。AC: モック private 継続→6。
- M12 --playlist 名指定: 自プレイリスト全ページ走査で完全一致・無ければ作成。playlistItems.list で重複追加防止。AC: 既存あり→insert未呼出／同名複数→exit 5／追加済み→skip。
- M13 Twitch取得: user_id 実行内キャッシュ・一覧 url 再利用（/videos?id= 再取得廃止）・cursor＋開始日時早期打切り・全 requests に timeout。AC: cursor 2ページ結合・範囲外除外・呼出回数アサーション。
- M14 長さ/サイズ: duration 解析は fullmatch・解析/取得不能は failed（fail-open禁止）。12h超はDLせず skip・DL後は実尺と st_size(256GB) 検査。AC: 43201秒でDL未呼出・duration不明 failed・サイズ超過はアップロード未呼出。
- M15 無人耐性: 接続/読込/総実行 deadline・有限リトライ（5xx/429 指数バックオフ）・SIGTERM ハンドラで台帳確定＋一時ファイル処理。AC: ハングモックで deadline→非ゼロ終了・SIGTERM→台帳整合。
- M16 多重起動防止（fcntl.flock）＋launchd plist 雛形（絶対venvパス/WorkingDirectory/ログパス事前作成/ThrottleInterval/KeepAliveなし）。AC: 二重起動で後発 exit 5。
- M17 一時ファイル: 名前に twitch_vod_id 必須・.part→原子的 rename。AC: 同日同タイトル別VODで衝突なし。
- M18 preflight（DL前）: title≤100文字・**description≤5000バイト(UTF-8)**（★6 設計差し戻しで字→バイトに統一・公式仕様準拠・2026-07-27 リーダー承認）・tags合計≤500文字・`<` `>` 禁止・description-file 存在・未知テンプレ変数。CLI不正→2・環境不備→5。空き容量チェック=想定ビットレート×尺×1.2。ffprobe 必須（不在 exit 5）。AC: 各境界値テスト。
- M19 ログ: logs/YYYY-MM-DD.log 1行1JSON（run_id・JST・保持期間規定）・yt-dlp 進捗抑制。AC: sentinel secret が stdout/stderr/ログに不出現・1実行ログ上限バイト内。
- M20 pytest 一式（socket遮断・全モック）。AC: pytest -q オフライン緑。
- M21 パス非依存（~/Workspace/Tools/twitch-vods-to-youtube/ へ移設前提）＋パッケージ化（pyproject＋console_scripts `vodp`）。AC: 任意 cwd から起動。

## D. 推奨(S)／任意(C)
S1 insert 100回/日バケット監視／S2 catch-up（--max-lookback-days）／S3 token pickle→JSON(authorized_user)・0600・旧pickle移行／S4 --check＝副作用ゼロ読取専用／S5 ruff/CI／S6 孤児掃除（台帳状態＋拡張子・ロック下・mtime基準）／S7 期待 channel_id 照合／S8 {episode} 自動採番。
C1 サムネイル／C2 チャプター／C3 多チャンネル／C4 通知／C5 --format 画質／C6 字幕／C7 プレイリスト順序。

## E. スコープ外
切り抜き生成／チャット焼き込み／Twitch側VOD削除・管理／多チャンネル同時運用／Kick・YouTube Live取込／リアルタイム録画／Twitch CSV取込(merge_dataset責務)／Vault記録(リーダー)／YouTube Analytics API（将来）。

## F. 汎用/固有の境界
汎用エンジン=VOD探索・DL・アップロード・公開・プレイリスト・台帳・CLI・終了コード・JSON契約。
固有=タイトル/説明テンプレ/タグ/categoryId/プレイリスト名/エピソード採番 → profiles/<name>.toml or CLI。エンジンソースに固有文字列を置かない（M9 grep AC）。

## G. 現状ギャップ（実コード確認済み・G1〜G21）
G1 --range 終端00:00（check_config.py にも同欠陥）／G2 main が sys.exit 不使用＝常に0／G3 台帳なし／G4 refresh失敗で creds=None のまま build() 継続・非対話でも run_local_server／G5 メタがハードコード（categoryId=22）／G6 プレイリスト機能なし／G7 構造化ログ・JSONなし／G8 テストゼロ／G9 get_videos が同一リクエスト30回＋/users 毎回再取得／G10 各VODで /videos?id= 追加取得・timeoutなし／G11 API失敗=0件と同一視／G12 duration が re.match 全optional＝fail-open／G13 例外握り潰し continue／G14 認証エラーで無限再帰しうる（YouTube・Twitch両方）／G15 token.pickle を本体・外部スクリプトが直読み／G16 app/ がディレクトリ依存 import でテスト困難／G17 cleanup_old_files 未呼出（README と乖離）／G18 run_upload.sh が | tee で終了コード握り潰し・無条件「完了しました」／G19 yt-dlp 進捗でログ巨大化／G20 README の .env.sample と実体 .env.example 不一致／G21 DLファイル名が日付_タイトルで衝突。

## H. OSS 先行調査 → 自作継続・部品採用なし
autovod(方式不一致)／TimIsOverpowered/archive(過剰インフラ)／porjo/youtubeuploader(不採用だが設計参照: headless token・playlistID・rate limit・private lock)／その他小規模・未メンテ。「翌日ルーチン＋段階台帳＋JSON契約＋非対話厳格失敗＋サブコマンド＋テスト」を満たす既存実装ゼロ。

## 追補（v1.1・補足3点の反映＋Codex差分10件全対応）

### M1改: サブコマンド完全化
- `vodp vods list / videos list / playlist list / upload / playlist create / playlist add / meta set / check / whoami / launchd generate`＋削除系。**全サブコマンドに引数表・出力 JSON Schema（`schemas/*.json` に固定）・終了コードを定義**。更新系も --json 必須。
- `videos list`＝channels.list(contentDetails)→uploads プレイリスト→playlistItems.list 全ページ（各1unit）。**search.list は使わない**（専用バケット100回/日＆結果不完全）。`playlist list`＝playlists.list(mine=true,50) 全ページ。
- 一覧系3種の AC: 外部リソース変更なし・台帳不変（token refresh 保存とログのみ許容）・ページネーション呼出列検証。`meta set` は現行 snippet を読んでマージ＝title/description/tags/categoryId を消失させない。
- **削除系は階層化**: `videos delete` / `playlist delete` / `playlist remove-item` / `cache clean`。**既定 dry-run・実行は `--yes` 必須**・--non-interactive で --yes 無しは exit 2・削除前に対象IDとタイトルを読み戻して出力。誤削除防止テスト必須。

### M22 新設: 説明文へのプレイリストURL自動挿入（冪等）
- --playlist 指定時 `プレイリスト: <URL>` を挿入。`{playlist_url}` は単独行のみ許可・無ければ末尾1行追加。冪等判定＝CRLF→LF 正規化＋行末空白除去後の完全一致。順序＝プレイリスト解決→insert（完全snippet・private）。
- 孤児プレイリスト防止: 説明文長（ID最大長見込み）を**作成前**に検証・5000字超過は作成せず exit 2。AC: 5000/5001境界・2回適用不変・既存行ありで追加なし。
- dry-run 例外の明文化: dry-run では読取API（playlists.list）のみ許可・書込/yt-dlp/台帳/FS変更は未呼出。未作成は playlist_url:null＋planned_playlist_title。M7 の AC を「書込API未呼出」に改訂。

### M21改: 移設耐性の具体化
- src/vodp/ に絶対パス禁止（grep AC・`! grep -R "/Users/" src/vodp`）・os.chdir 廃止（check_config.py:255）・**sh wrapper 廃止**（run_upload.sh の `| tee` 握り潰し=G18 の恒久対策。launchd は `vodp` を直接実行・`vodp launchd generate` を必須サブコマンド化）。
- 状態/ログ/認証の位置: `--home` > `VODP_HOME` > パッケージ既定。
- **venv は移動で壊れる**（pyvenv.cfg と shebang に絶対パスが焼かれる＝実測）→ 移設時は新パスで `.venv` を再作成＋`pip install -e .`（venv 名は `.venv` に統一）。AC-3b改: コピー先で旧venv除外→新規 .venv→一時 VODP_HOME＋オフラインで pytest 緑・任意 cwd から全サブコマンド動作。

## J. 未決 → リーダー裁定（2026-07-27・本人へは帰宅後報告）
①Twitch取得の共通化: **案B採用＝当面自前実装**（無人CLIの独立性優先・venv跨ぎ import は launchd で脆弱。「取得の正本=twitch-mcp」の本人指示は svwb 計測パイプラインのスコープと整理。将来の共有パッケージ化を課題登録。⚠️本人確認待ちフラグ）
②Codex#10 の却下＝**承認**（Glob でテスト不在確認済みは事実）
③--playlist-privacy 既定＝**public**（実運用が公開プレイリスト。CLI で上書き可）
④台帳ファイル＝**gitignore**（状態であってコードではない）
⑤--max-uploads 既定＝**5**
⑥sh wrapper＝**廃止**（本人の手癖になった手順が無い今が最安の移行時期・G18恒久対策。README に移行注記＝旧 `sh/run_upload.sh` 相当は `vodp vods upload`）
