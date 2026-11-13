# Validation Quickstart

```bash
uv sync --locked --extra dev
uv run bmem connect feishu
uv run bmem connect lark --no-history
uv run bmem feishu status
uv run bmem feishu sync
```

先启动独立数据目录服务并指定相同--home/--url。CLI首次选择历史范围，按官方URL或二维码完成授权。Web来源页选择品牌、范围、连接，与命令行状态一致。选空范围核验未发起业务历史读取。官方账号端验证需由使用者在浏览器完成授权，隔离自动化使用官方CLI协议替身。
