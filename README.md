# FDE SaaS Webhook Bridge（冪等性・リトライ・DLQ付き 外部連携アダプタ）

既存SaaS（kintoneやWebフォーム等）からのWebhook/API連携において、**スキーマ検証・冪等性キーによる二重処理防止・リトライ制御・デッドレターキュー（DLQ）隔離**を担う連携アダプタです。

```bash
python3 -m unittest discover -s tests -v
```
