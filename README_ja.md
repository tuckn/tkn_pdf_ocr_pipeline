# Tuckn PDF OCR — PDFを検索可能にするCLI

[English](README.md)

画像として保存されたPDFの文字をOCRで認識し、検索・コピーできるPDFを作成するコマンドラインツールです。
例えば、文字を選択できなかった書類で、文中の語句を検索したり、文章をコピーしたりできるようになります。

このCLIは、スキャン画像に、検索・コピー用の非表示文字情報（OCRテキストレイヤー）を追加します。元画像の再描画・再圧縮は行いません。

ページにOCRテキストレイヤーがすでに存在する場合は、通常はそのページを変更せず保持します。`<span>--redo-ocr</span>` を指定すると、古い非表示文字情報を除去し、新しく認識した文字情報に置き換えます。

Wordなどから出力された通常の文字があるページは、OCRせず保持します。これらが混在するPDFでは、対象の画像ページだけをOCRします。名前付きキューでは、OCRが不要な検索可能PDFも、そのまま後段へ引き継ぎます。

文字認識には **Azure Document Intelligence** のReadモデル（`prebuilt-read`）を使います。

複数の入力フォルダから、検証済みのPDFをそれぞれの出力フォルダへ保存できます。
入力PDFは既定で保持し、対象ごとに `after_success: delete` を指定すると、保存・検証・処理記録の保存後に削除します。
構造化データの抽出、発行日に基づくリネーム、最終保管先への移動は後段の処理で行います。

## 処理の流れ

`tkn-pdf-ocr run` でフォルダを処理するときの流れです。図の保存・削除は、各段階の検証が成功した場合に進みます。

```mermaid
sequenceDiagram
    autonumber
    participant CLI as tkn-pdf-ocr（PC）
    participant Input as 入力フォルダ
    participant State as 処理記録（PC）
    participant Azure as Azure Document Intelligence
    participant Output as 出力フォルダ

    Note over CLI,Output: run を実行（手動またはタスクスケジューラ）
    CLI->>CLI: 設定とフォルダの組み合わせを確認
    CLI->>Input: 対象のPDFを列挙
    loop PDFごとに処理
        CLI->>Input: PDFを読み込み、変更途中でないか確認
        CLI->>CLI: ページを分類し、OCR対象を判定
        CLI->>State: 過去の処理・引き渡し状況を確認
        alt dry-run
            CLI->>CLI: OCR・保存・入力削除の予定を表示
            Note over CLI,Output: 認証・Azure送信・書き込み・削除は行わない
        else 引き渡し完了済み（追加の後処理なし）
            CLI->>CLI: unchanged として処理を省略
            Note over State,Output: 後段で出力が移動されても再作成しない
        else 新しく引き渡すPDF
            alt OCRが必要
                CLI->>CLI: 対象ページだけの送信用PDFを作成
                CLI->>CLI: 認証（必要ならブラウザでログイン）
                CLI->>State: 送信予定を記録
                CLI->>Azure: 対象ページを送信してOCRを依頼
                Azure-->>CLI: 処理の受付情報
                CLI->>State: 再開に使う受付情報を保存
                loop OCRが完了するまで
                    CLI->>Azure: 処理状況を確認
                    Azure-->>CLI: 処理状況を返す
                end
                CLI->>Azure: 検索可能PDFを取得
                Azure-->>CLI: OCR結果のPDF
                CLI->>CLI: 非表示の文字情報を元PDFへ合成
                Note over CLI: 元画像を再描画・再圧縮せず保持
            else OCR不要の検索可能PDF
                CLI->>CLI: 元PDFをそのまま出力候補にする
            end
            CLI->>CLI: 出力候補と元PDFの整合性を検証
            CLI->>State: 引き渡し予定を記録
            CLI->>Output: PDFを保存し、保存内容を再確認
            CLI->>State: 保存・検証済みとして記録
            alt after_success が delete
                CLI->>CLI: 入力・出力が変わっていないか再確認
                CLI->>Input: 元PDFを削除
            else after_success が keep（既定）
                Note over CLI,Input: 元PDFを保持
            end
            CLI->>State: 引き渡し完了を記録
        end
    end
    CLI->>State: 実行結果を保存（通常実行のみ）
    CLI->>CLI: 件数と各PDFの処理結果を表示
```

判定できないPDFや、OCR対象ページから文字を取得できなかったPDFは、確認が必要な結果として入力を保持し、新しい出力を保存しません。
図では中断・エラーからの復旧を省略しています。再実行時は保存済みの受付情報からOCRを再開し、入力削除だけが残っている場合はOCRせず削除を再試行します。
詳しくは[処理仕様](docs/reference/processing.md#recovery-and-state)を参照してください。

## 利用前に確認すること

**OCR対象のPDFはAzureへ送信され、文字の解析・認識に料金がかかります。**
ただし、`--dry-run` では処理対象をローカルで確認するだけで、Azureへの送信や課金、ファイルへの書き込みは行いません。
料金は[Azure Document Intelligenceの料金表](https://azure.microsoft.com/en-us/pricing/details/document-intelligence/)で確認できます。

必要なものは次のとおりです。
以下の手順はWindowsのPowerShellを使用します。LinuxでのCLIの動作は未検証です。

- Python 3.11以上と[uv](https://docs.astral.sh/uv/)。
- Azure Document Intelligenceリソース。API `2024-11-30` の検索可能PDF出力に対応している必要があります。
- Webブラウザと、対象リソースの **Cognitive Services User** ロールを持つMicrosoft Entraアカウント。Azure CLIの導入・ログインは不要です。

Azureリソースと権限は、あらかじめ用意してください。
このCLIはAzureリソースを作成・デプロイしません。

## インストールする

リポジトリのフォルダでインストールし、ヘルプが表示されることを確認します。
例のパスは、手元のリポジトリの場所に置き換えてください。

```powershell
cd "C:\path\to\tkn_pdf_ocr_pipeline"
uv tool install .
tkn-pdf-ocr --help
```

## 設定する

### 1. 設定ファイルを作成する

```powershell
tkn-pdf-ocr config init
```

表示された `path` の設定ファイルを開きます。
既定の保存先は `~/.tkn/pdf_ocr_pipeline/config.yaml` です。`~` はユーザーのホームフォルダを表します。

### 2. 処理対象・保存先・接続先を指定する

`sources` が未指定、または設定を統合した結果が `{}` の場合、`default` という対象で次のフォルダを使います。

- 入力: `~/.tkn/pdf_ocr_pipeline/data/incoming`
- 出力: `~/.tkn/pdf_ocr_pipeline/data/searchable`

入力フォルダを作成してPDFを置いてください。出力フォルダは処理時に作成します。設定確認と `--dry-run` ではフォルダを作成しません。
既定では入力を保持し、サブフォルダは処理せず、ファイル名も変えません。`run --source default` で指定できます。
独自の `sources` を設定した場合は、その対象だけを処理し、既定の対象を追加しません。Azureの接続先設定は別途必要です。

以下は設定ファイル全体として使える最小構成です。
生成されたファイルをそのまま使う場合は、対応する項目を編集してください。

```yaml
schema_version: "3.0.0"
sources:
  receipts:
    enabled: true
    recursive: false
    input_dir: C:/path/to/receipts/1_rawPDF
    output_dir: C:/path/to/receipts/2_ocrPDF
    after_success: delete
    output_suffix: "_ocr"
  catalogs:
    enabled: false
    recursive: true
    input_dir: C:/path/to/catalogs/1_rawPDF
    output_dir: C:/path/to/catalogs/2_ocrPDF
    after_success: keep
    output_suffix: "_ocr"
azure:
  endpoint: https://<resource-name>.cognitiveservices.azure.com
  auth_mode: browser
```

| 項目               | 設定する内容                                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `input_dir`      | OCR対象のPDFがあるフォルダ。                                                                  |
| `output_dir`     | OCR後のPDFを保存するフォルダ。                                                                |
| `azure.endpoint` | 利用するAzureリソースのエンドポイント。`<resource-name>` を実際のリソース名に置き換えます。 |

`sources` の各項目に入力・出力フォルダを指定します。`receipts` などのIDは処理履歴の識別に使うため、運用開始後は変更しないでください。
この例の `receipts` は、検証後に入力を削除します。残す場合は `after_success: keep` にします。省略時も `keep` です。
`output_suffix: "_ocr"` により `receipt.pdf` は `receipt_ocr.pdf` になります。省略時は同じ名前です。
追加対象はフォルダを用意してから `enabled: true` にします。
入力・出力フォルダは `sources.<ID>` 内だけに指定します。トップレベルの `input_dir` / `output_dir` は受け付けません。

有効な対象すべての入力・出力・処理記録のフォルダは、別のフォルダにし、相互に配下へ置かないでください。
出力PDFを入力として再び処理することや、元PDFへの上書きを避けるための制約です。
ブラウザ認証では、上記のようなリソース固有のサブドメインを持つエンドポイントを使います。

### 3. 設定を確認する

保存した設定が反映されているか確認します。

```powershell
tkn-pdf-ocr config show
```

`settings` に有効な設定、`winning_sources` に各値の取得元が表示されます。
このコマンドは設定の確認用で、Azureへの接続確認は行いません。

### 4. ブラウザでAzureにログインする

```powershell
tkn-pdf-ocr auth login
```

初回はブラウザが開くので、対象リソースを使えるアカウントでログインします。
このコマンドは認証だけを行い、PDFの送信・OCR課金は発生しません。リソースへのアクセス権は実際のOCR時に確認されます。
ログインを省略した場合も、最初のOCR時に必要に応じてブラウザが開きます。

次回からはアプリ専用の暗号化キャッシュを使い、再認証が必要になった場合だけブラウザを開きます。
アカウントを選び直すには `tkn-pdf-ocr auth login --reauthenticate` を使います。
`auth login --dry-run` と `config show` は、ブラウザ起動や認証キャッシュへのアクセスを行いません。

利用先のテナントを指定する場合は `azure.tenant_id` を設定します。
無人実行用の認証やAPIキーを使用する場合は、[認証方式の設定](docs/reference/configuration.md#azure-options)を参照してください。

## PDFを検索可能にする

`run` は有効なすべての `sources` を処理します。`run --source receipts` で特定の対象だけを実行できます。
それぞれの `input_dir` にあるPDFを処理し、指定した接尾辞を付けて `output_dir` に保存します。
ページごとに判定し、画像だけのページへOCR文字を追加します。通常の文字があるページとOCR済みのページは保持します。OCR済みページの認識をやり直す場合は[再OCR](#文字認識をやり直す)を使います。

まず、Azureへ送信せずに処理対象を確認します。

```powershell
tkn-pdf-ocr run --dry-run
```

結果の `planned` が処理予定の件数です。`source_action: delete` は、検証成功後に入力を削除する予定を示します。
`--dry-run` ではPDFや記録の保存、入力削除は行いません。
対象を確認したら、OCRを実行します。この実行ではPDFをAzureへ送信します。

```powershell
tkn-pdf-ocr run
```

OCR後のPDFは `output_dir` に保存されます。
コンソールには作成件数や失敗件数が表示されます。出力がない場合は、[実行結果の読み方](#実行結果の読み方)を確認してください。

コピーや更新の途中にあるファイルを読み込むことを避けるため、最終更新時刻から30秒未満のPDFは処理を見送ります。
該当する場合は、ファイルの更新が終わって30秒以上経過してから再実行してください。
待機時間は `min_age_seconds` で変更できます。

## 用途に応じた使い方

### 1つのPDFを指定する

`convert INPUT --output OUTPUT` で、処理対象と保存先を直接指定します。`--output` は必須です。
パスを実際のファイルに置き換えて実行してください。

```powershell
tkn-pdf-ocr convert "C:\path\to\document.pdf" --output "C:\path\to\document-searchable.pdf"
```

### サブフォルダも処理する

対象の `sources.<ID>` 内に `recursive: true` を指定します。省略時は `false` です。

```yaml
sources:
  receipts:
    input_dir: C:/path/to/receipts/1_rawPDF
    output_dir: C:/path/to/receipts/2_ocrPDF
    recursive: true
```

対象ごとに設定でき、トップレベルの `recursive` やCLIの一括指定はありません。

`input_dir` 内のサブフォルダも対象にし、フォルダ構造を `output_dir` に引き継ぎます。

### 追加したPDFを処理する

`tkn-pdf-ocr run` を再実行します。
名前付きキューでは、後段が出力を移動しても、完了した入力のパスと内容を記録して再生成を防ぎます。
処理条件や接尾辞を変更しても、同じ入力を再処理しません。再OCRが必要な場合は、保持しているPDFに対して `convert` と明示的な出力先を使います。
単体変換の `convert` では、元PDF・処理条件・出力の内容を使って完了判定します。[再処理の条件](docs/reference/configuration.md#reprocessing-consequences)を参照してください。

### 文字認識をやり直す

`--redo-ocr` を付けると、スキャン画像に重なる古い非表示文字だけを除去し、新しく認識した非表示文字を追加します。

| ページの内容                                   | 通常の実行              | `--redo-ocr` 指定時        |
| ---------------------------------------------- | ----------------------- | ---------------------------- |
| 紙をスキャンした画像だけ                       | 非表示のOCR文字を追加。 | 同じ。                       |
| スキャン画像と非表示のOCR文字                  | OCR済みとして保持。     | 古い非表示文字を除去・置換。 |
| Wordなど由来の通常の文字                       | OCRせず保持。           | OCRせず保持。                |
| 白紙・画像のないページ・安全に判定できない構造 | 保持。                  | 保持。                       |

混在PDFでは、対象の画像ページだけをAzureへ送信し、その他のページは元のまま出力に含めます。
名前付きキューでは、すでに検索可能なPDFはAzureへ送信せず、バイト列を変えずにコピーして、同じ検証・入力保持方針を適用します。
判定不能なページ、文字が抽出できない文字ページ、OCR対象も文字もないPDF、OCR対象ページの一部でも文字が得られないPDFは `needs_review` とし、入力を残します。新しい出力は保存しません。白紙のスキャンもこの確認対象になります。
`convert` は、対象ページがなければスキップします。
同じページに通常の文字と画像が混在する場合は、そのページ全体を保持します。

```powershell
tkn-pdf-ocr convert "C:\path\to\document.pdf" --output "C:\path\to\document-searchable.pdf" --redo-ocr
```

元画像の再描画・再圧縮は行いません。Azureの返すPDFから非表示文字とフォントを取り出し、元PDFへ追加します。
ページ寸法、画像、通常の描画、しおり、リンク、注釈、メタデータを引き継ぎます。
PDFファイル全体のバイト列の一致や、電子署名の有効性、PDF/A・アクセシビリティへの適合を保証するものではありません。
元PDFへの上書きは行いません。入力削除は、名前付きキューで `after_success: delete` を指定した場合だけです。

置換対象は標準的な非表示文字（描画モード3）です。別形式の非表示表現や代替テキストなど、判定が不確かなページは `unsupported` として保持します。[対応範囲](docs/reference/processing.md#page-classification-and-limits)を参照してください。
`--redo-ocr` は実行のたびに指定します。設定ファイルで常時有効にする項目はありません。

実サンプル20件・21ページを新方式でAzure OCRし、元画像のデータと描画結果の一致を確認しました。出力容量は合計で元の約1.11倍です。
選んだ100項目はすべて検索できました。全文の認識率を示すものではありません。[検証記録](docs/validation.md)に方法と範囲を記載しています。

### 保存先に同名のPDFがある場合

内容が異なる、または処理記録で確認できない既存PDFは、上書きせずエラーになります。
`--overwrite` を付けると、既存PDFを同じフォルダの `.bak-<id>` ファイルへバックアップしてから置き換えます。
元PDFを保存先に指定することはできません。`--overwrite` だけではOCR済みページの再OCRを有効にしません。必要な場合は `--redo-ocr --overwrite` と両方を指定します。

完了済みで内容が一致するPDFは、`--overwrite` を付けても `unchanged` です。
同じ元PDF・同じ処理条件でOCRをやり直す場合は、新しい保存先を指定してください。

### 中断した処理を再開する

同じコマンドを再実行すると、記録済みのAzureの処理IDを使って再開します。
Azureが要求を受け付けたか不明な場合や、結果の取得期限が切れた場合は停止します。
その場合は[再開・再送信の手順](docs/reference/processing.md#recovery-and-state)を確認してください。
`--retry-uncertain` による再送信には、追加料金がかかる可能性があります。

### フォルダを定期的に処理する

このCLIには、常駐してファイルの追加を検知し、自動で処理する機能はありません。
定期的に処理する場合は、Windowsではタスクスケジューラ、Linuxでは[cron](https://man7.org/linux/man-pages/man5/crontab.5.html)などから `tkn-pdf-ocr run` を実行します。
LinuxでのCLIの動作は未検証です。

Windowsのタスクスケジューラでは、次の内容を設定します。

| 項目             | 設定                                                                |
| ---------------- | ------------------------------------------------------------------- |
| プログラム       | `Get-Command tkn-pdf-ocr` で確認した `tkn-pdf-ocr.exe` のパス。 |
| 引数             | `run`。                                                           |
| 開始するフォルダ | 手動実行で使用した作業フォルダ。                                    |
| 実行ユーザー     | 手動実行と同じ設定・認証を利用できるWindowsユーザー。               |
| 多重起動         | 前回の処理が続いている場合は、新しい処理を開始しない設定。          |

PCの電源が入り、スリープしていない間に実行できます。
ブラウザ認証はキャッシュが有効な間は再利用できますが、再認証時にはユーザー操作が必要です。
継続的な無人実行には、[無人実行用の認証](docs/reference/configuration.md#azure-options)を設定してください。

## 入力削除と途中停止からの再開

`after_success: delete` は、出力の検証・保存・保存済みファイルのハッシュ確認・処理記録の保存後に、入力が変わっていないことを再確認して削除します。
ごみ箱への移動ではなく、ファイルの削除です。確認するのはローカルの保存結果で、OneDriveクラウド側の同期完了は確認しません。

削除だけ失敗した場合は `cleanup_pending` になります。同じコマンドを再実行すると、OCRをやり直さず削除処理を再開します。
ただし、出力が移動・変更されて確認できなければ `needs_review` とし、入力削除も出力再生成も行いません。
保存直前・直後の中断で出力の有無を確定できない場合も同様です。後段のファイルを確認し、必要なら記録された出力を元の場所へ戻してください。通常の再試行のために履歴を消さないでください。
削除直後に完了記録だけが残せなかった場合は、次回実行で入力が存在しないことを確認して記録を復旧します。

## 実行結果の読み方

進捗やエラーはコンソールの標準エラーへ、最終結果は標準出力へJSONで表示します。
`run` と `convert` の結果には、次の件数が含まれます。

| 項目                | 意味                                                                          |
| ------------------- | ----------------------------------------------------------------------------- |
| `created`         | 新しく保存したPDF。                                                           |
| `replaced`        | バックアップ後に置き換えたPDF。                                               |
| `unchanged`       | 完了済みの内容と一致し、再処理しなかったPDF。                                 |
| `skipped`         | 条件により処理を見送ったPDF。`files` 内の `reason` で理由を確認できます。 |
| `planned`         | `--dry-run` で処理予定になったPDF。                                         |
| `failed`          | 処理に失敗したPDF。`files` 内の `error` で原因を確認できます。            |
| `needs_review`    | 検証や引き渡し状態の確認が必要なPDF。入力は保持します。                       |
| `cleanup_pending` | 出力保存後、入力の削除だけが未完了のPDF。                                     |

`skipped` の理由が `no_eligible_pages` ならOCR対象ページがありません。`page_kinds` にページ順の判定（`scan`、`ocr_text`、`native_text`、`no_scan_image`、`unsupported`）、`ocr_pages` にOCR対象のページ番号が表示されます。`input_not_stable_yet` は最終更新からの待機時間を満たしていない場合です。
`sources` に対象ごとの件数、`files` に `source_id` と個別結果が表示されます。
`failed: 0` でも、`needs_review` と `cleanup_pending` を確認してください。
すべてのPDFが `skipped` または `unchanged` なら、新しいPDFは作成されません。

`--dry-run` 以外では、実行レポートもファイルに保存します。
保存先は結果の `run_report` に表示されます。強制終了などでレポートが残らない場合があります。

終了コードは、`0` が正常終了または処理対象なし、`1` が1件以上のファイル失敗・要確認・削除未完了、`2` が引数・設定などのエラー、`130` が中断です。

## コマンド一覧

コマンド名の前に `tkn-pdf-ocr` を付けて実行します。

| 目的                                 | コマンド                                               |
| ------------------------------------ | ------------------------------------------------------ |
| 設定ファイルを作る                   | `config init [PATH] [--dry-run] [--force]`           |
| 有効な設定と取得元を確認する         | `config show`                                        |
| 1つのPDFをOCRする                    | `convert INPUT --output OUTPUT`                      |
| `input_dir` のPDFをまとめてOCRする | `run [--source ID]`                                  |
| PDFのページ数と文字の有無を調べる    | `verify INPUT [--expected-pages N] [--require-text]` |

各コマンドの `--help` でオプションを確認できます。
共通の `--config PATH`、`--quiet`、`--verbose` はサブコマンドの前後に指定できます。
`--quiet` は進捗を省いてエラーと結果のJSONを表示し、`--verbose` は診断情報を追加します。

## 設定ファイルと処理記録の保存先

設定は次の順に読み込み、後の値を優先します。`azure` と `sources.<ID>` 内の項目も個別に統合します。

1. 組み込みの既定値。
2. `~/.tkn/pdf_ocr_pipeline/config.yaml`。
3. 実行時フォルダの `./.tkn/config.yaml`。
4. `--config` で指定したファイル。
5. コマンドに指定した個別のオプション。

相対パスの基準は、設定ファイルの場所ではなく、コマンドを実行したフォルダです。
`config init` は既存の編集済み設定を保護します。`--force` を付けると、バックアップしてから置き換えます。
設定項目・形式・優先順位の詳細は[設定仕様](docs/reference/configuration.md)を参照してください。

再開用の処理記録、実行レポート、多重実行を防ぐロックファイルは、既定で `~/.tkn/pdf_ocr_pipeline/state/` に保存します。
処理記録を削除すると、完了済みの判定や中断後の再開ができなくなるため、再試行のために削除しないでください。
記録にはファイルのパスやハッシュが含まれます。OCRで認識した本文は保存しません。
ブラウザ認証のアカウント識別情報は `~/.tkn/pdf_ocr_pipeline/authentication/` に保存します。
アクセストークン等はOSで暗号化するアプリ専用キャッシュへ保存し、設定ファイルや処理記録へ書き込みません。
保存構造と復旧への影響は[処理記録の仕様](docs/reference/processing.md#recovery-and-state)を参照してください。

## PDFの検査と制限

OCR後、保存前にCLIがPDFを読み取り、元PDFとのページ数・ページ寸法の一致を確認します。
画像データとその解釈に必要な情報が変化していないことも確認します。Azureが文字を検出したページでは、PDFから文字を抽出できることを確認します。
これらはファイルの構造と文字の有無の検査であり、認識した文字が正しいかの判定ではありません。

既存のPDFを調べるには `verify` を使えます。
`--require-text` を付けると、1ページも文字を抽出できない場合にエラーになります。
この検査のためにAzureへ送信することはありません。

- 処理対象はPDFです。暗号化されているPDFや、構造が不正なPDFは送信前にエラーになります。
- 既定の上限は1ファイル2,000ページ、500 MiBです。Azure側にもプランごとの上限があります。無料のF0では先頭2ページまでのため、3ページ以上を処理する場合はS0が必要です。[サービスの上限](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0)も確認してください。
- PDF全体とAzureの返すPDFをメモリに読み込みます。大きな文書には十分なメモリが必要です。
- 白紙など、Azureが文字を検出しなかったページには文字が付かない場合があります。名前付きキューではOCR対象ページに文字が得られない場合は要確認として入力を保持します。`convert` では、Azureが単語を検出していなければ警告付きで保存します。
- ページの判定は文字の描画命令・描画モード・画像の有無に基づきます。フォント情報の欠落など、解釈できない構造は保持またはエラーになります。
- すべてのPDF閲覧ソフトでの表示や、アクセシビリティ、署名、PDF/Aへの適合は、上記の検査では確認できません。

再送信の扱い、保存中の競合、強制終了後の一時ファイルの処置などは[処理仕様](docs/reference/processing.md)を参照してください。
複数のCLIから同時に使う場合は、同じ `state_dir` を使用します。
保存先には、ハードリンクとファイルの原子的な置換に対応したローカルファイルシステムが必要です。他のアプリによる同時編集を完全に防ぐことはできません。

## CLIを更新する

リポジトリを更新した後、以下の操作で再インストールして変更を反映します。

```powershell
cd "C:\path\to\tkn_pdf_ocr_pipeline"
uv tool install . --reinstall
tkn-pdf-ocr --version
```

フォルダ処理の設定は `sources` に統一しています（設定形式 `3.0.0`）。フォルダが1組の場合も1つの項目として指定します。
`sources` が未設定なら、`run` は上記の既定フォルダを使います。単体変換の `convert INPUT --output OUTPUT` はフォルダ設定なしで利用できます。

## 開発する

開発時にソースの変更を直接反映する場合は、`uv tool install -e . --reinstall` を使います。
テストとパッケージの確認は次の手順で行います。

```powershell
cd "C:\path\to\tkn_pdf_ocr_pipeline"
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

バージョンごとの追加・変更は[変更履歴](CHANGELOG.md)、検証方法・結果・確認範囲は[検証記録](docs/validation.md)を参照してください。

テストは合成PDFとAzureの応答を模したデータを使います。
日本語PDFを使った実OCRの確認範囲と結果は[検証記録](docs/validation.md)を参照してください。

アプリケーションは[MITライセンス](LICENSE)です。
依存ライブラリの用途とライセンスは[実装構成](docs/reference/processing.md#implementation-boundaries)に記載しています。
