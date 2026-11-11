---
name: ios-release-guard
description: 個人の iOS アプリを Claude Code に TestFlight まで通させるときの「止める条件」と「成功の証拠」を固定する。アーカイブ・書き出し・アップロード・処理完了の確認までを扱い、審査提出はしない。「TestFlight に上げて」「リリースビルドを作って」「提出前に確認して」と頼まれたときに使う。
when_to_use: xcodebuild archive / exportArchive / altool upload / App Store Connect のビルド確認を行うとき
disable-model-invocation: false
license: MIT
---

# iOS Release Guard

このスキルが決めるのは「いつ止めるか」と「何を見たら成功と言ってよいか」。作業そのものはプロジェクトの手順（Makefile、fastlane、README など）に従う。手順が無ければ、作業の前に人間に聞く。

## 0. 先に埋める値（人間が書く。秘密そのものは書かない）

| 項目 | 値 |
|---|---|
| プロジェクト / ワークスペース | `<App>.xcodeproj` |
| スキーム | `<Scheme>` |
| Bundle ID | `<bundle id>` |
| ExportOptions.plist の場所 | `<path>` |
| App Store Connect API キーを読む env ファイル | `<path to .env>`（中身を表示しない） |
| ログと成果物の置き場所 | `<作業用ディレクトリ>`（リポジトリの外） |

値が空のまま作業を始めない。

## 1. 止める条件（見つけたら作業をやめて報告する。回避策を試さない）

1. **証明書を作る・失効させる操作が必要になった。**
   配信証明書はチームに属し、種類ごとに 1 つしか持てない（Developer ID を除く）。失効させると、提出前のアップロード済みビルドが Invalid Binary になりうる。
   出典: Apple「Certificates overview」 https://developer.apple.com/help/account/certificates/certificates-overview/
   署名エラーを直すための新しい証明書の発行は、別のアプリの出荷を止める操作になりうる。人間の判断に戻す。
2. **キーチェーンのパスワード入力ダイアログが出た（または出ている気配がある）。**
   非対話の実行では画面に見えないまま止まる。`ps aux | grep -c '[S]ecurityAgent'` が 1 以上なら止める。
3. **自分が触っていない変更が作業ツリーにある。** `git status --porcelain` が空でなければ、何を出荷するのか確定しない。
4. **秘密を表示しそうになった。** API キー、Issuer ID、Key ID、キーチェーンのパスワードを `cat`・`echo`・ログに出さない。env ファイルは `source` で実行しない（中身がシェルとして実行される）。必要な値だけを変数に読み、表示しない:
   `ASC_KEY_ID="$(grep -m1 '^ASC_KEY_ID=' <env> | cut -d= -f2-)"`
5. **審査提出・リリース・Apple への返信に進もうとしている。** このスキルの範囲外。

## 2. 成功の証拠（これ以外を「成功」の根拠にしない）

| 段 | 証拠として引用する行・値 | 証拠にならないもの |
|---|---|---|
| アーカイブ | ログファイル内の `** ARCHIVE SUCCEEDED **` | 終了コード 0 だけ、`grep error` が空だったこと |
| 書き出し | `** EXPORT SUCCEEDED **` と、生成された `.ipa` のパスと更新時刻 | 以前から置いてある `.ipa` |
| テスト（ある場合。無ければ報告に「なし」） | `Executed N tests, with 0 failures` と `** TEST SUCCEEDED **` | 「テスト通過」という要約、0 件実行 |
| Release 起動 | Release 構成のビルドを新しいシミュレーターに入れ、30 秒後もプロセスが生きていることと最初の画面のスクリーンショット | Debug での起動、ユニットテストの通過 |
| アップロード後 | App Store Connect API の `GET /v1/builds` で、該当ビルドの `processingState` が `VALID` | `altool` の「アップロード成功」表示 |

`processingState` の値は `PROCESSING` / `FAILED` / `INVALID` / `VALID`。Apple はこの値を「テストに使える状態になっていないことを示す処理状態」と説明している。
出典: Apple「Build.Attributes」 https://developer.apple.com/documentation/appstoreconnectapi/build/attributes-data.dictionary

**grep で絞った結果が空でも、「成功」と「何も出ていない」を区別できない。** ログは必ずファイルに全部書き、判定は上の行の有無で行う。報告にはその行をそのまま引用する。

## 3. 手順

1. 「0. 先に埋める値」がすべて埋まっていることと、「1. 止める条件」に当たらないことを確かめる。
2. ビルド番号を上げる。App Store Connect の最新ビルド番号より大きいことを API で確かめる（`GET /v1/builds?filter[app]=<appId>&sort=-version&limit=1`）。
3. テストがあれば実行する（`xcodebuild test ... > "<作業用>/test.log" 2>&1`）。`Executed N tests, with 0 failures` の N が 0 なら、テストは通っていないものとして扱う。その後アーカイブする。長いビルドはバックグラウンドで実行し、ログはファイルへ:
   ```sh
   xcodebuild -scheme "<Scheme>" -configuration Release \
     -archivePath "<作業用>/App.xcarchive" archive > "<作業用>/archive.log" 2>&1
   ```
4. **Release 起動チェック。** 自分専用のシミュレーターを作り（`xcrun simctl list devicetypes` と `xcrun simctl list runtimes` で値を選び、`xcrun simctl create <名前> <デバイス種別> <ランタイム>`。出力が UDID）、以降は UDID を指定する。`simctl shutdown all` は使わない（他の作業のシミュレーターも止める）。Release 構成でビルドしてインストールし、起動して 30 秒待ち、スクリーンショットを撮る。Release 構成でしか通らない処理（例: Release 専用の起動時ガード）は、Debug のテストでは確かめられない。
5. 書き出す（`xcodebuild -exportArchive ... -exportOptionsPlist <path>`）。ログはファイルへ。
6. 下の「4. ビルドの中身の点検」を `.ipa` に対して行う。
7. 検証してからアップロードする。API キー認証。値は env から読み、表示しない:
   ```sh
   xcrun altool --validate-app -f <ipa> -t ios --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
   xcrun altool --upload-app   -f <ipa> -t ios --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
   ```
   （`--apiKey` の秘密鍵ファイルの置き場所は `man altool` を確認する。）
8. API で `processingState` を読む。`PROCESSING` の間は数分おきに読み直す。`VALID` になるまで「完了」と言わない。`INVALID` / `FAILED`、該当ビルドが 1 件に特定できない、API がエラーを返した、知らない値が返った、のいずれも成功扱いせず止めて報告する。`VALID` は「処理が終わった」の意味で、テスターへの配布（ベータグループへの割り当てなど）は別の作業。
9. 「5. 報告の形」で報告する。自分が作ったビルド成果物とシミュレーターを片付ける（`xcrun simctl delete <UDID>`）。

## 4. ビルドの中身の点検（`.ipa` を展開して見る）

ソースではなく、出荷する成果物を見る。

1. **プライバシーマニフェストの整合。** `.app/PrivacyInfo.xcprivacy` を `plutil -lint` と `plutil -p` で読む。`NSPrivacyTracking` が `true` なのに `NSPrivacyTrackingDomains` が空配列なら無効なマニフェスト。`false` なのにドメインが入っている場合も無効。
   出典: Apple「TN3181: Debugging an invalid privacy manifest」 https://developer.apple.com/documentation/technotes/tn3181-debugging-invalid-privacy-manifest
   （私の場合、この状態はアップロードと TestFlight の処理を通り、審査に出した時点で初めて弾かれた。）
2. **テスト用広告 ID が入っていないか。** Google Mobile Ads を使うなら、ビルドされた `Info.plist` の `GADApplicationIdentifier` と、実行ファイル内の文字列に Google のサンプル用 ID（`ca-app-pub-3940256099942544`）が無いことを確かめる（`strings <実行ファイル> | grep -c ca-app-pub-3940256099942544` が 0）。
   出典: Google「Enable test ads」 https://developers.google.com/admob/ios/test-ads
3. **アイコンが入っているか。** `.app` に `Assets.car` があり、`Info.plist` に `CFBundleIcons` 系のキーがあること。私の場合、XcodeGen が未知の設定キーを黙って捨て、アーカイブは成功したまま `Assets.car` の無いアプリができた。
4. **埋め込みフレームワークの版番号。** `.app/Frameworks/*.framework/Info.plist` すべてに `CFBundleShortVersionString` があること。
5. **点検したものを書く。** 各項目について「何を開いて、何件見たか」を書く。0 件を見て「問題なし」と書かない。

## 5. 報告の形（1 回の作業につき 1 ブロック）

```
アプリ: <名前> / ビルド: <版>(<番号>) / コミット: <SHA>
ARCHIVE: 「** ARCHIVE SUCCEEDED **」 (<ログのパス>)
EXPORT:  「** EXPORT SUCCEEDED **」 (<ipa のパス>, <更新時刻>)
TEST: 「Executed N tests, with 0 failures」「** TEST SUCCEEDED **」（テストが無ければ「なし」と書く）
Release 起動: 30 秒生存 (<スクリーンショットのパス>)
中身の点検: マニフェスト <件数> 件 lint OK / テスト広告 ID 0 件（実行ファイル <本数> 本）/ Assets.car <件数> 件 / フレームワーク版番号 <確認数>/<総数>
processingState: VALID（<読んだ時刻>）
止めた理由: なし（あれば「1. 止める条件」の何番か）
```

## このスキルに入っていないもの

- 審査提出、メタデータ、App Privacy の申告、課金アイテム、配信地域の設定。
- App Store Connect API の認証クライアント本体。トークンの作り方は Apple「Generating tokens for API requests」 https://developer.apple.com/documentation/appstoreconnectapi/generating-tokens-for-api-requests に従って用意する。
- 私が実際に踏んだ提出まわりの罠 57 本と、提出前チェックリスト・自動化スクリプト 7 本は、Zenn の本「個人開発者のApp Store提出 — 実測した罠57本と提出前チェックリスト」にまとめている: https://zenn.dev/dhubplus/books/ios-submission-traps-and-scripts
