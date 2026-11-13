# Data Model

## Connection Config

每个品牌的配置包含profile名称、history_confirmed、sources、enabled、最近成功同步时间。账号指纹保存在事件与游标命名空间，进程重建后从官方CLI重新确认用户身份。JSON原子写入用户数据目录connections/，权限受用户目录控制。凭据与device_code不进入该文件。

## Runtime State

phase、installed、cli_version、authenticated、account、authorization、sync、error、busy。授权字段只含URL、到期与二维码入口，内部device_code在进程结束时清除。连接完成后清除挑战。

## Import Cursor

复用import_cursors，以品牌/账号/类别/对象组成source/path，last_cursor保存JSON水位，分页或对象失败前已提交的事件重放幂等。

## Remote Event

harness=feishu或lark，source_type=external，sensitivity默认private，显式云端许可时normal，metadata.origin=feishu-remote，metadata.history_import=true。事件身份由品牌、应用用户指纹、对象ID、版本/正文指纹及分块序号构成，原始时间由官方字段提供。原文以T4/episode引用快照可检索，保留当前来源信任，规则候选仍隔离。只读内容不通过declare入口。
