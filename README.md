# PriceWatch

给 TrueNAS 准备的小型价格监控网页：优先识别美国戴尔的 Alienware 商品，记录配置、美元价格与历史，变化时才通过飞书机器人通知。也支持部分提供标准结构化商品数据的其他商店。深浅色界面、单管理员、SQLite，不需要 Redis。

> 已在 TrueNAS 25.10 安装并确认网页健康接口、首次注册页正常；从 TrueNAS 命令行请求参考戴尔页面返回 403，浏览器兜底仍待实测。**尚未验证戴尔价格和飞书实发**，不要据此做购买决策。首次使用请按下方验收步骤核对。

## 在 TrueNAS 安装

需要 x86-64 Docker/Compose、联网和一个可写数据集。i3-12100 可运行，但 Chromium 兜底会比普通抓取占用更多内存；实际峰值待 TrueNAS 测量。

1. 将此项目放到 TrueNAS 可执行 Compose 的位置，并在 TrueNAS 创建一个专用持久数据集，例如 `/mnt/POOL/apps/pricewatch`。让容器用户 `10001:10001` 对该数据集有读写权限。不要把数据集放在 Git 仓库里。
2. 复制 `.env.example` 为 `.env`；将 `PRICEWATCH_DATA_PATH` 改成上述绝对路径。用 `openssl rand -hex 32` 生成一次 `PRICEWATCH_APP_SECRET_KEY`，将结果只写入 `.env`，不要提交到 GitHub。**以后保持这个密钥不变**，否则已加密的飞书配置无法解密。
3. 在项目目录运行 `docker compose up -d --build`。默认只监听本机 `127.0.0.1:8080`。在同机浏览器访问 `http://127.0.0.1:8080`，首次创建一个管理员账号。网页注册只开放这一次。
4. 若反向代理不在 TrueNAS 本机，先把 `.env` 中的 `PRICEWATCH_BIND` 改为仅反代能访问的 TrueNAS 局域网地址，再配置反代。对公网务必使用 HTTPS，将 `.env` 的 `PRICEWATCH_EXTERNAL_URL` 设为完整 HTTPS 地址，再执行 `docker compose up -d`。在反代传递 `Host`、`X-Forwarded-Proto`，并限制管理页面的公开访问范围。若代理与容器同网络但不在主机回环上，需要按实际网络拓扑调整端口绑定。

Compose 默认本地构建 `pricewatch:local`。将来有经过验收的 GHCR 版本时，可以在 `.env` 的 `PRICEWATCH_IMAGE` 改为明确版本（例如 `ghcr.io/你的账号/pricewatch:1.0.0`），先 `docker compose pull`，再 `docker compose up -d`。不要依赖 `latest` 回滚。

## 初次验收

1. 在“通知与设置”粘贴飞书群自定义机器人的完整 Webhook，保存后点击“发送测试消息”。如果机器人设置了关键词或签名，请先按飞书侧规则完成配置；本应用目前只支持无签名的标准 Webhook。
2. 添加戴尔美国站参考链接：`https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250/aa18250_reg_01`。确认预览中的型号、SKU、CPU/GPU、内存、存储、价格与浏览器看到的公开页面一致；有差异就不要确认，反馈页面截图或脱敏 HTML。
3. 确认后检查首次飞书消息只收到一次。再点击“立即检查”，价格及状态未变化时应保持安静。飞书 Webhook 由你在网页中填写，本项目和 GitHub 不保存它的明文。

默认按北京时间 04:00、10:00、16:00、22:00 检查。每件商品可在详情页设为 1、3、6、12、24 小时；最多建议监控十件。戴尔页面链接变化时，应用验证 SKU 与配置后才更新可信链接；有歧义会暂停比较并提示人工确认。其他商店识别属于尽力支持，不保证网页都能抓到。

目标价仅在价格变化并首次跌破所设阈值时，附加在该次价格提醒中；设置时如果当前价已低于目标，不会因为价格没变而补发提醒。

## 备份、恢复与保留时间

商品和价格历史默认永久保留，归档后仍保留；只有在归档后明确输入 `DELETE` 才永久删除。已发送通知记录保留 90 天，检查诊断记录保留 30 天。每天自动备份，保留最近 7 个每日备份和 4 个每周备份；升级前另建一份不参与自动轮转的备份。建议同时用 TrueNAS 快照保护整个数据集。

“通知与设置”可以手动备份并下载（网页下载上限 64 MiB；大文件直接从 TrueNAS 数据集取）。备份包含加密的配置和历史，请妥善保存 `.env` 的原始密钥。恢复是离线操作：

1. `docker compose stop pricewatch`，并确认没有另一个 PriceWatch 进程正在使用此数据集。
2. 在数据集的 `backups` 目录找好 SQLite 备份和页面列出的 SHA-256 校验码；或者先运行 `sha256sum` 自行核对。
3. `docker compose run --rm --no-deps --entrypoint pricewatch pricewatch backup restore --file /data/backups/备份文件名.sqlite3 --confirm-checksum 校验码`。恢复程序会先对当前数据库创建 `pre_restore` 备份。
4. `docker compose up -d`，再检查 `/healthz`、管理员登录和历史记录。

若新镜像与旧 schema 不兼容，先停止服务，按上面步骤恢复与旧镜像对应的 `pre_migration` 备份，再将 `PRICEWATCH_IMAGE` 固定为旧版本并启动。切勿在不恢复数据库的情况下盲目降级 schema。

## 状态与排查

- `docker compose ps` 和 `docker compose logs --tail=100 pricewatch` 可看容器状态，`curl -f http://127.0.0.1:8080/healthz` 可看健康检查。日志不要包含完整飞书 Webhook；分享日志前仍建议检查脱敏。
- 戴尔返回 403 或出现人机验证时，应用可能启动受限 Chromium 重试；不会绕过验证码。请从 TrueNAS 上先用普通浏览器确认网页能正常显示、价格是否与匿名/地区会话一致。
- 反代登录循环一般与 HTTPS/外部地址及 Cookie 有关，检查 `PRICEWATCH_EXTERNAL_URL` 和反代转发头。
- 忘记密码时，先停止服务；把密码写入权限为 600 的数据集文件，再运行 `docker compose run --rm --no-deps --entrypoint pricewatch pricewatch admin reset-password --username 用户名 --password-file /data/密码文件名`。重置后删除此明文文件，不要把密码写进命令历史。
- 用 `docker stats` 记录真实空闲内存、浏览器检查峰值；用 `docker image ls` 记录镜像大小。目前尚未测量浏览器抓取峰值，不提供未经测量的数字。

## GitHub 发布

项目以 MIT 许可证公开：[GitHub 仓库](https://github.com/shudaizi520/pricewatch)。GHCR 镜像尚未发布；先让 CI 在 Python 3.12 和 Docker 构建上通过，并完成 TrueNAS 实机验收。稳定版发布工作流需要配置受保护的 `production` environment、仓库变量 `RELEASE_SIGNING_PUBLIC_KEY`、签名的 `v1.0.0` 类 tag；人工触发工作流后发布 `linux/amd64` 的 `1.0.0`、`1.0`、`1` 和 `latest`。不要将 `.env` 或真实 Webhook 推上去。

CI 会完整报告镜像的高危/严重漏洞，并阻止任何已有修复版本却尚未升级的漏洞。2026-09-21 的试构建仍有 56 项 Debian 系统包告警（其中 1 项严重），扫描器尚未列出修复版本。这不是“零漏洞”；待上游发布修复后应及时重建和升级镜像。实机验收期间只应经受信任的局域网或反代访问，不应直接把 8080 暴露到公网。

依赖/许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
