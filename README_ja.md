# Tuckn PDF OCR — PDFを検索可能にするCLI

[English](README.md)

画像として保存されたPDFの文字をOCRで認識し、検索・コピーできるPDFを作成するコマンドラインツールです。
例えば、文字を選択できなかった書類で、文中の語句を検索したり、文章をコピーしたりできるようになります。

このCLIは、スキャン画像に、検索・コピー用の非表示文字情報（OCRテキストレイヤー）を追加します。元画像の再描画・再圧縮は行いません。

ページにOCRテキストレイヤーがすでに存在する場合は、通常はそのページを変更せず保持します。`<span>--redo-ocr</span>` を指定すると、古い非表示文字情報を除去し、新しく認識した文字情報に置き換えます。

Wordなどから出力された通常の文字があるページは、OCRせず保持します。これらが混在するPDFでは、対象の画像ページだけをOCRします。対象ページがなければ、PDF全体をスキップします。

文字認識には **Azure Document Intelligence** のReadモデル（`prebuilt-read`）を使います。

OCR後のPDFは元のPDFと別に保存します。

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

以下は設定ファイル全体として使える最小構成です。
生成されたファイルをそのまま使う場合は、対応する項目を編集してください。

```yaml
schema_version: "2.0.0"
input_dir: 'C:\path\to\incoming-pdf'
output_dir: 'C:\path\to\searchable-pdf'
azure:
  endpoint: https://<resource-name>.cognitiveservices.azure.com
  auth_mode: browser
```

| 項目               | 設定する内容                                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------- |
| `input_dir`      | OCR対象のPDFがあるフォルダ。                                                                  |
| `output_dir`     | OCR後のPDFを保存するフォルダ。                                                                |
| `azure.endpoint` | 利用するAzureリソースのエンドポイント。`<resource-name>` を実際のリソース名に置き換えます。 |

`input_dir` と `output_dir` は別のフォルダにし、一方をもう一方の配下に置かないでください。
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

`run` は `input_dir` のPDFを処理し、同じファイル名で `output_dir` に保存します。
ページごとに判定し、画像だけのページへOCR文字を追加します。通常の文字があるページとOCR済みのページは保持します。OCR済みページの認識をやり直す場合は[再OCR](#文字認識をやり直す)を使います。

まず、Azureへ送信せずに処理対象を確認します。

```powershell
tkn-pdf-ocr run --dry-run
```

結果の `planned` が処理予定の件数です。
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

`convert` では、処理対象と保存先を直接指定できます。
パスを実際のファイルに置き換えて実行してください。

```powershell
tkn-pdf-ocr convert "C:\path\to\document.pdf" --output "C:\path\to\document-searchable.pdf"
```

### サブフォルダも処理する

```powershell
tkn-pdf-ocr run --recursive
```

`input_dir` 内のサブフォルダも対象にし、フォルダ構造を `output_dir` に引き継ぎます。

### 追加したPDFを処理する

`tkn-pdf-ocr run` を再実行します。
元PDF、処理条件、保存済みPDFの内容が前回と一致するものは、Azureへ再送信せず `unchanged` になります。
処理条件を変更すると再処理の対象になる場合があります。[再処理の条件](docs/reference/configuration.md#reprocessing-consequences)を参照してください。

### 文字認識をやり直す

`--redo-ocr` を付けると、スキャン画像に重なる古い非表示文字だけを除去し、新しく認識した非表示文字を追加します。

| ページの内容                                   | 通常の実行              | `--redo-ocr` 指定時        |
| ---------------------------------------------- | ----------------------- | ---------------------------- |
| 紙をスキャンした画像だけ                       | 非表示のOCR文字を追加。 | 同じ。                       |
| スキャン画像と非表示のOCR文字                  | OCR済みとして保持。     | 古い非表示文字を除去・置換。 |
| Wordなど由来の通常の文字                       | OCRせず保持。           | OCRせず保持。                |
| 白紙・画像のないページ・安全に判定できない構造 | 保持。                  | 保持。                       |

混在PDFでは、対象の画像ページだけをAzureへ送信し、その他のページは元のまま出力に含めます。
対象ページがなければPDF全体をスキップし、出力ファイルは作りません。
同じページに通常の文字と画像が混在する場合は、そのページ全体を保持します。

```powershell
tkn-pdf-ocr convert "C:\path\to\document.pdf" --output "C:\path\to\document-searchable.pdf" --redo-ocr
```

元画像の再描画・再圧縮は行いません。Azureの返すPDFから非表示文字とフォントを取り出し、元PDFへ追加します。
ページ寸法、画像、通常の描画、しおり、リンク、注釈、メタデータを引き継ぎます。
PDFファイル全体のバイト列の一致や、電子署名の有効性、PDF/A・アクセシビリティへの適合を保証するものではありません。
元ファイル自体は変更しません。

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

## 実行結果の読み方

進捗やエラーはコンソールの標準エラーへ、最終結果は標準出力へJSONで表示します。
`run` と `convert` の結果には、次の件数が含まれます。

| 項目          | 意味                                                                          |
| ------------- | ----------------------------------------------------------------------------- |
| `created`   | 新しく保存したPDF。                                                           |
| `replaced`  | バックアップ後に置き換えたPDF。                                               |
| `unchanged` | 完了済みの内容と一致し、再処理しなかったPDF。                                 |
| `skipped`   | 条件により処理を見送ったPDF。`files` 内の `reason` で理由を確認できます。 |
| `planned`   | `--dry-run` で処理予定になったPDF。                                         |
| `failed`    | 処理に失敗したPDF。`files` 内の `error` で原因を確認できます。            |

`skipped` の理由が `no_eligible_pages` ならOCR対象ページがありません。`page_kinds` にページ順の判定（`scan`、`ocr_text`、`native_text`、`no_scan_image`、`unsupported`）、`ocr_pages` にOCR対象のページ番号が表示されます。`input_not_stable_yet` は最終更新からの待機時間を満たしていない場合です。
`failed: 0` でも、すべてのPDFが `skipped` または `unchanged` なら、新しいPDFは作成されません。

`--dry-run` 以外では、実行レポートもファイルに保存します。
保存先は結果の `run_report` に表示されます。強制終了などでレポートが残らない場合があります。

終了コードは、`0` が正常終了または処理対象なし、`1` が1件以上のファイル失敗、`2` が引数・設定などのエラー、`130` が中断です。

## コマンド一覧

コマンド名の前に `tkn-pdf-ocr` を付けて実行します。

| 目的                                 | コマンド                                               |
| ------------------------------------ | ------------------------------------------------------ |
| 設定ファイルを作る                   | `config init [PATH] [--dry-run] [--force]`           |
| 有効な設定と取得元を確認する         | `config show`                                        |
| 1つのPDFをOCRする                    | `convert INPUT --output OUTPUT`                      |
| `input_dir` のPDFをまとめてOCRする | `run [--recursive]`                                  |
| PDFのページ数と文字の有無を調べる    | `verify INPUT [--expected-pages N] [--require-text]` |

各コマンドの `--help` でオプションを確認できます。
共通の `--config PATH`、`--quiet`、`--verbose` はサブコマンドの前後に指定できます。
`--quiet` は進捗を省いてエラーと結果のJSONを表示し、`--verbose` は診断情報を追加します。

## 設定ファイルと処理記録の保存先

設定は次の順に読み込み、後の値を優先します。`azure` 内の項目も個別に統合します。

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
- 白紙など、Azureが文字を検出しなかったページには文字が付かない場合があります。PDF全体に文字がなくても、Azureが単語を検出していなければ警告付きで保存します。
- ページの判定は文字の描画命令・描画モード・画像の有無に基づきます。フォント情報の欠落など、解釈できない構造は保持またはエラーになります。
- すべてのPDF閲覧ソフトでの表示や、アクセシビリティ、署名、PDF/Aへの適合は、上記の検査では確認できません。

再送信の扱い、保存中の競合、強制終了後の一時ファイルの処置などは[処理仕様](docs/reference/processing.md)を参照してください。
複数のCLIから同時に使う場合は、同じ `state_dir` を使用します。
保存先には、ハードリンクとファイルの原子的な置換に対応したローカルファイルシステムが必要です。他のアプリによる同時編集を完全に防ぐことはできません。

## 更新する

リポジトリを更新した後、再インストールして変更を反映します。

```powershell
cd "C:\path\to\tkn_pdf_ocr_pipeline"
uv tool install . --reinstall
tkn-pdf-ocr --version
```

0.1.0の設定で `azure.auth_mode: azure_cli` を指定していた場合は、`browser` に変更してください。
0.3.0では `schema_version: "2.0.0"` に更新し、廃止された `existing_text` と `redo_dpi` を各設定ファイルから削除してください。旧オプションも廃止し、必要な再OCRは `--redo-ocr` で指定します。
画像保持方式への変更により、0.1.0/0.2.0の処理記録は新方式の完了判定に使いません。旧記録・出力はそのまま残ります。新しい保存先を指定するか、既存出力を置き換える場合は `--overwrite` を併用してください。

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

テストは合成PDFとAzureの応答を模したデータを使います。
日本語PDFを使った実OCRの確認範囲と結果は[検証記録](docs/validation.md)を参照してください。

アプリケーションは[MITライセンス](LICENSE)です。
依存ライブラリの用途とライセンスは[実装構成](docs/reference/processing.md#implementation-boundaries)に記載しています。
