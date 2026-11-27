# 007 发行验收与渠道回读

`.github/workflows/release-candidate.yml` 在六个原生目标构建候选并保存构建身份。`.github/workflows/windows-update-acceptance.yml` 在 Windows x64 和 ARM64 分别执行 U08 与计划任务接管。工作流校验 `0.2.0` 的 Rust 和 Web Git 树，保留原始审核提交与可检出快照提交的标识，再记录架构、三份程序及各自日志的 SHA-256。计划任务用例需要仍处于登录状态的空白 Windows 用户配置。失败或跳过仍会上传可取得的日志。[GitHub 原生运行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) · [Windows 交互任务定义](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)

## GitHub 公开预览版

从已审核的提交运行 `release-candidate.yml`，将 `public_preview` 设为 `true`。六个原生作业使用同一提交构建，分别运行 Rust 测试和首装、工作台授权、智能体授权回归。收集六个产物中的程序和 `*.record.json`，置于同一目录，再生成上传附件。

```sh
python3 scripts/release/preview.py \
  --assets /path/to/verified-builds --output /path/to/new-release-attachments \
  --tag v0.3.0-preview.1 --commit "$release_commit"
```

将 tag 固定到产物的提交，在 GitHub Release 草稿上传六个压缩包与 `SHA256SUMS`。也可运行 `preview-draft.yml`，输入六目标构建的 run ID 和已推送的 tag，由托管运行器核对来源并暂存草稿。校验资料、资产与签名状态，确认标记为 **Pre-release** 后发布。用匿名下载重新核对六个包的字节。公开预览的用户手动下载和安装。详情见[用户说明](../../docs/quick-start.md)。

## 正式发布授权与配置

| 配置 | 交付给发布维护者的内容 |
|---|---|
| GitHub | 审核后的正式构建提交、运行六目标 Actions 的授权、操作不可变 Release 与 GitHub Pages 的执行人。发布环境只向经过批准的身份开放。[不可变 Release 权限](https://docs.github.com/en/rest/repos/repos?apiVersion=2022-11-28#enable-immutable-releases) · [Pages 部署权限](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) |
| macOS | 两架构使用的 Developer ID Application Team ID、可用的签名及公证入口。使用 `.pkg` 分发时同时提供 Developer ID Installer 身份。裸 CLI 核对签名类型、Team ID 与在线公证票据，首装容器按自身格式验证 Gatekeeper。[Apple 公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow) |
| Windows | Authenticode 签名服务或受控签名主机、该次签名证书的 SHA-256 和 RFC 3161 时间戳服务。使用 Azure Artifact Signing 时提供区域端点、账户及证书 profile，由授权身份在该 profile 上签名。[Microsoft 签名配置](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations) |
| TUF | 获准的公开 `root.json` 原始字节与 SHA-256，以及 `root`、`targets`、`snapshot`、`timestamp` 的角色阈值、签名方式和续签负责人。将公开根原文注入 `BMEM_OFFICIAL_TUF_ROOT`。Linux 两制品的信任由签名目录绑定其最终摘要。[TUF 规范](https://theupdateframework.github.io/specification/latest/) |

私钥和访问凭据留在签名环境。公开根和已签元数据可进入交付目录。稳定渠道使用 GitHub Pages 的 `/botinkit-mem/update/` 路径。

## 申请正式签名

| 目标 | 007 需要的信任材料 | 获取方式 |
|---|---|---|
| macOS arm64、x64 | Developer ID Application 签名及 Apple 公证。首装若采用 `.pkg`，另签 Developer ID Installer | 以发布主体加入 Apple Developer Program。组织入会需法定主体、D‑U‑N‑S 编号和具授权的人员。在签名 Mac 的钥匙串生成 CSR，由 Account Holder 在 Certificates, Identifiers & Profiles 中申请 Developer ID Application，再把下载的 `.cer` 导入同一钥匙串。Apple 列出的会费为每年 99 美元，实际按所在地区结算。[加入计划](https://developer.apple.com/cn/programs/enroll) · [创建证书](https://developer.apple.com/help/account/certificates/create-developer-id-certificates) · [生成 CSR](https://developer.apple.com/help/account/certificates/create-a-certificate-signing-request/) |
| Windows x64、ARM64 | 公共信任的 Authenticode 签名，附 RFC 3161 时间戳 | 发布主体符合地区要求时，可以在 Azure 创建 Artifact Signing 账户，完成发布主体身份验证，创建 **Public Trust** 证书 profile，并为签名身份授予 Certificate Profile Signer。中国大陆主体目前未列入该服务的 Public Trust 支持地区，可向 Windows 信任的 CA 申请组织代码签名证书，先确认主体资格、硬件令牌或托管签名方式。DigiCert CertCentral 加 KeyLocker 是一种托管路径。[Azure 申请与地区](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart) · [Azure 签名集成](https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations) · [CA 托管申请](https://docs.digicert.com/en/digicert-keylocker/overview/access.html) |
| Linux x64、ARM64 | 稳定 TUF 目录认证最终二进制的长度、SHA-256 和 HTTPS 下载地址 | 维护者签发本项目的 TUF 目标目录。按现有 007 流程，Linux 记录使用 `--signer tuf-target`。[TUF 规范](https://theupdateframework.github.io/specification/latest/) |
| 全平台 `bmem update` | 正式 TUF 根与 `root`、`targets`、`snapshot`、`timestamp` 四角色签名 | 发布维护者批准正式根、签名阈值、轮换和续签责任。离线保管根私钥，分别控制发布目录和在线续签权限。正式签名环境自建密钥，不涉及额外平台证书采购。将批准的 `root.json` **原文**注入 `BMEM_OFFICIAL_TUF_ROOT`，按原始字节核对 SHA-256。[TUF 密钥角色](https://theupdateframework.github.io/specification/latest/#key-management-and-migration) |

### 平台签名与验签

macOS 签名主机使用钥匙串内的 Developer ID Application 身份，开启 Hardened Runtime 并请求安全时间戳。分别处理两个架构，等公证状态为 `Accepted`，再按 Team ID 复核最终裸 CLI。裸二进制和 ZIP 的票据在线查询。若交付 `.pkg`，给已签名的扁平安装包公证后可用 `xcrun stapler` 附票并复验。[Apple 公证工作流](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow) · [签名与公证要求](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)

```sh
codesign --sign "$developer_id_identity" --options runtime --timestamp "$bmem_binary"
ditto -c -k --norsrc "$bmem_binary" "$notary_zip"
xcrun notarytool submit "$notary_zip" --keychain-profile "$notary_profile" --wait
codesign --verify --all-architectures --strict --check-notarization "$bmem_binary"
```

Windows 在签名环境通过供应商的 SignTool 集成对两份 `.exe` 进行 Authenticode 签名，文件摘要和 RFC 3161 时间戳摘要都用 SHA-256。授权签名人记录本次签发证书的 **SHA-256 指纹**，验收时把预期指纹传给 `candidate.py record --signer`，并运行 `signtool verify /pa /all /tw <exe>`。托管证书轮换时，发布记录按本次签名回执更新。[Microsoft SignTool](https://learn.microsoft.com/windows/win32/seccrypto/signtool) · [托管签名资源与角色](https://learn.microsoft.com/en-us/azure/artifact-signing/concept-resources-roles)

### 从内测进入正式发布

1. 从已审核提交重新构建六个生产目标，固定正式版本与 TUF 根。编译使用 `--release --locked`，关闭 `internal-release`、`update-test` 与 `evaluation-clock`。用 `build-info` 核对 `test_build=false`、正式根 SHA-256、目标架构和提交。
2. 完成平台签名后再登记摘要并跑原生验收。按下节生成稳定目录，在获准的正式签名环境签发元数据。仓库的 `internal_channel.py` 只负责内测目录。
3. 发布执行人先准备不可变 Release 的草稿及六个最终资产。离线验收成功后公开 Release，逐个回读资产、标签和摘要。部署稳定渠道时先放版本化元数据与目标目录，最后启用 `timestamp.json`，再从公开地址复测 `bmem update --check` 和实际升级。[不可变 Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

已受管的内测安装仍信任编译时的内测根。当前 `update` 沿用原信任渠道，`install --adopt` 的兼容入口针对 0.2.0。内测迁往稳定渠道需单独验证同实例交接与失败回退。给内测包补平台签名会改变制品字节，且内测构建身份仍会被正式制品检查拒绝。[升级信任配置](../../src/v2/bmem/src/update/catalog.rs) · [正式制品检查](candidate.py)

## 0.3.0 内部测试

内部编译使用 `--release --locked --features internal-release`。`BMEM_INTERNAL_TUF_ROOT` 指向公开根文件，`BMEM_BUILD_COMMIT` 指向验收用的完整源码提交。内部程序在 `build-info` 报告根指纹和测试构建标记。验收专用后继使用 `BMEM_BUILD_VERSION=0.3.1` 单独构建。该特性与故障注入和冻结时钟互斥。

用 `internal_channel.py init --keys` 在本机专用目录建立四角色密钥，通过 `--root` 另存公开根。用 `record` 记录实际候选的构建和文件摘要，用 `publish` 生成一个本地签名目录。暂存后继使用 `--previous` 绑定原一代的候选字节并递增元数据版本。`verify` 用独立 Python TUF 客户端和事先获得的公开根校验目录和全部目标文件。部分架构交付应在 `publish` 与 `verify` 明确列出每个 `--target`，默认要求六目标齐全。密钥目录在本地渠道和制品归档之外。[TUF 规范](https://theupdateframework.github.io/specification/latest/)

在测试机使用 `BMEM_INTERNAL_TUF_REPOSITORY` 指向已验证的本地目录。0.3.0 资产可以复制为独立的 `bmem-installer`，由其完成空目录安装或 `--adopt` 接管选定的原 `0.2.0` 实例。安装后用 `bmem update --check`、`bmem update` 和 `bmem update status`。切到较新签名目录后可做连续升级，持久缓存拦截旧元数据重放。[Spec Kit 007 场景及复跑方式](../../specs/007-bmem-release-update/quickstart.md)

## 验证交付物

1. 从同一已审核提交构建六个候选。生产构建启用获准的公开 TUF 根，关闭 `internal-release`、`update-test` 与 `evaluation-clock`。先在各平台完成签名，再用 `candidate.py record --target … --signer …` 重新记录最终字节。macOS 使用 Developer Team ID，Windows 使用 Authenticode 证书 SHA-256 指纹，Linux 使用 `--signer tuf-target`。[Apple 公证](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution) · [Microsoft SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)
2. 以各目标最终签名制品完成 U01–U08 适用项和 U10，并核验实际首装入口。将 `<target>.record.json`、`<target>.acceptance.json` 和六个最终制品放入同一交付目录。验收记录包括 `asset_sha256`、`scenarios` 和 `regression`。`candidate.py catalogue` 核对六个目标、根指纹、制品字节与各目标验收记录。U09 另存渠道核验回执。[007 验收列表](../../specs/007-bmem-release-update/quickstart.md)
3. 发布执行人将最终字节上传到 `v0.3.0` 草稿，重新下载六份资产，再用相同记录核对。用缺件和字节不一致的临时目录验证草稿检查会失败，并核对有效稳定 timestamp 当时的状态。签制 TUF 元数据后，用下面的离线核验检查暂存的 `metadata/` 与 `targets/`。现有渠道递增已签版本，首发按已审核 TUF 根的角色和阈值建立渠道。[GitHub 草稿发布](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) · [python-tuf 7.0.1](https://github.com/theupdateframework/python-tuf/releases/tag/v7.0.1)

```sh
python3.11 -m venv /path/to/release-venv
/path/to/release-venv/bin/python -m pip install 'tuf==7.0.1' 'cryptography==50.0.2'

python3 scripts/release/candidate.py catalogue \
  --assets /path/to/final-assets --root /path/to/approved-root.json \
  --version 0.3.0 --commit "$release_commit" \
  --output /path/to/signing-input/release-catalog.json

/path/to/release-venv/bin/python scripts/release/verify_release.py \
  --assets /path/to/final-assets --root /path/to/approved-root.json \
  --version 0.3.0 --commit "$release_commit" \
  --stage /path/to/signed-tuf-tree --offline --output /path/to/offline-receipt.json
```

## 稳定渠道验收

授权人员公开不可变 GitHub Release 后，去掉 `--offline`，保留 `--stage` 并写入新的回执。验证器走公开网络，确认实际标签提交、Release 的不可变状态、六个资产 URL 和完整下载字节，再用获准 TUF 根验证待启用的目录。回执含发布 ID、各资产 SHA-256、签名目录 SHA-256 和 TUF 元数据版本。[GitHub Release API](https://docs.github.com/en/rest/releases/releases#get-a-release-by-tag-name) · [GitHub 资产回读](https://docs.github.com/en/rest/releases/assets#get-a-release-asset)

暂存与公开资产回读均通过后，由发布执行人在 Pages 上部署版本化元数据和目标文件，最后启用指向该目录的 `timestamp.json`。启用后去掉 `--stage`，用新的回执从实际公开稳定渠道重复核验。在验收实例运行 `bmem update --check`、`bmem update` 和 `bmem update status`，核对目标版本、构建身份及状态。将 U09 的草稿失败演练、公开资产和公开稳定渠道回执与各目标验收记录一起归档。[更新命令契约](../../specs/007-bmem-release-update/contracts/update.md)

## 本地回归

```sh
BMEM_UPDATE_TEST_OPENSSL=/path/to/openssl3 \
  /path/to/release-venv/bin/python -B tests/v2/release/test_release_verification.py -v
```

本地用例用真实临时 Ed25519 签名验证 TUF 目录，用合成 GitHub 响应覆盖不可变性、标签、缺失资产和完整传输字节。原生签名和公开稳定渠道有各自的验收记录。
