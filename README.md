# PriceWatch

给 TrueNAS 准备的小型价格监控网页：优先识别美国戴尔的 Alienware 商品，记录配置、美元价格与历史，变化时才通过飞书机器人通知。也支持部分提供标准结构化商品数据的其他商店。深浅色界面、单管理员、SQLite，不需要 Redis。

> 已在 TrueNAS 25.10 安装。2026-09-21 从同一 NAS 的隔离容器验证：美国戴尔 Alienware 18 Area-51 默认 RTX 5070 报价 $3,999.99；选中 RTX 5090、确认戴尔自动调整的电源等配件后，页面实际报价 $5,099.99，SKU 为 `aa18250_reg_01`。另实测 CherryMX 键盘选中后仍为 $3,999.99；相同价格也必须由戴尔配置报价请求确认。这些价格来自当时公开网页，不是长期保证。正式应用健康检查、数据库迁移、既有卡片/历史数量和升级前备份校验已通过；**飞书签名模式仍待管理员填入密钥后实发验收**。

## 从 GitHub 镜像空白安装

`compose.ghcr.yml` 是独立的拉镜像安装文件：不需要下载源码、Dockerfile 或 `.env`。公开的 `ghcr.io/shudaizi520/pricewatch:1.0.1` 已发布；需要联网的 x86-64 Docker/Compose 主机。

1. 把 [compose.ghcr.yml](compose.ghcr.yml) 保存到一个新目录，在该目录运行 `docker compose -f compose.ghcr.yml pull`，再运行 `docker compose -f compose.ghcr.yml up -d`。
2. 默认在安装设备本机打开 `http://127.0.0.1:8080`；首次填写管理员账号、密码、确认密码，以后只填账号和密码。不需要额外的“注册密钥”。
3. 程序自动生成内部随机密钥，保存在 Docker 数据卷内的 `/data/.pricewatch-secret`，并和数据库一起跨容器重启保留。它不是账户密码，不会写进日志、页面或 GitHub。正常安装不用查看它；只有未来要搬迁**旧数据**时，才需要把整个 `/data` 数据卷（包含密钥）一起备份。

这是一份**空白安装**：在另一台电脑重新安装会得到新账号和空商品列表，原来的商品、历史价格、飞书设置不会从 GitHub 自动恢复。Docker 跑在电脑上时，电脑关机就停止监控；放在常开的 NAS 上则不依赖电脑在线。若需要从局域网另一台设备访问，把 YAML 中端口绑定的 `127.0.0.1` 改成 Docker 主机的局域网 IP；不要直接把管理页面暴露到公网。以后更新时先把 YAML 的镜像标签改成已发布的**明确版本**，再运行 `docker compose -f compose.ghcr.yml pull` 和 `docker compose -f compose.ghcr.yml up -d`；同目录下的命名数据卷会保留。戴尔美国站若直连遇到 403，仍需按下文为安装设备配置可用的 SOCKS5 代理；空白安装本身不需要代理。

## 在 TrueNAS 安装

需要 x86-64 Docker/Compose、联网和一个可写数据集。i3-12100 可运行；为兼容美国戴尔商品页，镜像内含完整 Chromium 和虚拟显示，仍是单容器应用。浏览器兜底比普通抓取占用更多内存；实际峰值待 TrueNAS 测量。

1. 将此项目放到 TrueNAS 可执行 Compose 的位置，并在 TrueNAS 创建一个专用持久数据集，例如 `/mnt/POOL/apps/pricewatch`。让容器用户 `10001:10001` 对该数据集有读写权限。不要把数据集放在 Git 仓库里。
2. 复制 `.env.example` 为 `.env`；将 `PRICEWATCH_DATA_PATH` 改成上述绝对路径。用 `openssl rand -hex 32` 生成一次 `PRICEWATCH_APP_SECRET_KEY`，将结果只写入 `.env`，不要提交到 GitHub。**以后保持这个密钥不变**，否则已加密的飞书配置无法解密。
3. 在项目目录运行 `docker compose up -d --build`。默认只监听本机 `127.0.0.1:8080`。在同机浏览器访问 `http://127.0.0.1:8080`，首次填写账号、密码和确认密码来创建管理员。网页初始化只开放这一次。
   当前 TrueNAS 试用实例在本地镜像重建失败后，临时将 `./src` 只读挂载到容器的 `/app/src`。需要复现时，可显式启用 `docker-compose.source.yml` 覆盖文件；正式使用版本化镜像升级或回退时不要启用此覆盖文件，以免宿主机源码覆盖镜像代码。依赖变更仍须重建镜像。
4. 若反向代理不在 TrueNAS 本机，先把 `.env` 中的 `PRICEWATCH_BIND` 改为仅反代能访问的 TrueNAS 局域网地址，再配置反代。对公网务必使用 HTTPS，将 `.env` 的 `PRICEWATCH_EXTERNAL_URL` 设为完整 HTTPS 地址，再执行 `docker compose up -d`。在反代传递 `Host`、`X-Forwarded-Proto`，并限制管理页面的公开访问范围。若代理与容器同网络但不在主机回环上，需要按实际网络拓扑调整端口绑定。

如果美国戴尔对 NAS 的直连请求返回 403，可选填 `PRICEWATCH_DELL_SOCKS_PROXY_HOST`（NAS 容器可访问的代理 IP）和 `PRICEWATCH_DELL_SOCKS_PROXY_PORT`。两项必须一起设置；只用于美国戴尔的浏览器取价，不影响其他商店，也不会改变每张卡片的检查时间。应用仍先校验目标域名解析得到的公网 IP，再通过 SOCKS5 代理连接；代理不可用时会报告失败，不会偷偷改走直连或写入猜测价格。若代理无需认证且监听局域网，务必在软路由防火墙中只允许 NAS 访问该端口，切勿向公网开放。

原 `docker-compose.yml` 默认本地构建 `pricewatch:local`，保留给源码部署；从镜像空白安装请使用上面的独立 `compose.ghcr.yml`，不要与 `docker-compose.source.yml` 混用。不要依赖 `latest` 回滚。

## 初次验收

1. 在“通知与设置”粘贴飞书群自定义机器人的完整 Webhook；若机器人开启“签名校验”，再填写其签名密钥。保存后点击“发送测试消息”。密钥与 Webhook 均加密保存，网页不会回显；不要发到聊天或提交 GitHub。
2. 添加戴尔美国站参考链接：`https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250`。如需跟踪不同显卡或键盘，点击“选择戴尔配置”，选择选项，再核对网页最终显示的价格及自动变更的配件。相同链接的不同配置会保存为不同卡片；不要把默认配置价格当成其它配置的价格。有差异就不要确认，反馈页面截图或脱敏 HTML。
3. 确认后检查首次飞书消息只收到一次。新卡片默认不自动检查；在卡片右上角选择北京时间，再打开自动开关。单卡手动刷新不受自动开关影响，价格及状态未变化时应保持安静。飞书 Webhook 由你在网页中填写，本项目和 GitHub 不保存它的明文。

每张卡片可以设置自己的每日检查时间（北京时间），自动开关只控制这件商品；同一检查时间不能同时用于两件开启自动检查的商品。卡片上的刷新图标只手动检查该商品；“状态”页显示上次/下次检查及最近记录。最多建议监控十件。戴尔页面链接变化时，应用验证 SKU 与配置后才更新可信链接；有歧义会暂停比较并提示人工确认。其他商店识别属于尽力支持，不保证网页都能抓到。

目标价仅在价格变化并首次跌破所设阈值时，附加在该次价格提醒中；设置时如果当前价已低于目标，不会因为价格没变而补发提醒。

## 备份、恢复与保留时间

商品和价格历史默认永久保留，归档后仍保留；只有在归档后明确输入 `DELETE` 才永久删除。已发送通知记录保留 90 天，检查诊断记录保留 30 天。每天自动备份，保留最近 7 个每日备份和 4 个每周备份；升级前另建一份不参与自动轮转的备份。建议同时用 TrueNAS 快照保护整个数据集。

“通知与设置”可以手动备份并下载（网页下载上限 64 MiB；大文件直接从 TrueNAS 数据集取）。备份包含加密的配置和历史：源码部署要保存 `.env` 的原始密钥，镜像空白安装则要保存 `/data/.pricewatch-secret`；只恢复数据库而丢失原密钥无法解密飞书配置。恢复是离线操作：

1. `docker compose stop pricewatch`，并确认没有另一个 PriceWatch 进程正在使用此数据集。
2. 在数据集的 `backups` 目录找好 SQLite 备份和页面列出的 SHA-256 校验码；或者先运行 `sha256sum` 自行核对。
3. `docker compose run --rm --no-deps --entrypoint pricewatch pricewatch backup restore --file /data/backups/备份文件名.sqlite3 --confirm-checksum 校验码`。恢复程序会先对当前数据库创建 `pre_restore` 备份。
4. `docker compose up -d`，再检查 `/healthz`、管理员登录和历史记录。

若新镜像与旧 schema 不兼容，先停止服务，按上面步骤恢复与旧镜像对应的 `pre_migration` 备份，再将 `PRICEWATCH_IMAGE` 固定为旧版本并启动。切勿在不恢复数据库的情况下盲目降级 schema。

## 状态与排查

- `docker compose ps` 和 `docker compose logs --tail=100 pricewatch` 可看容器状态，`curl -f http://127.0.0.1:8080/healthz` 可看健康检查。日志不要包含完整飞书 Webhook；分享日志前仍建议检查脱敏。
- 戴尔返回 403 或出现人机验证时，应用会尝试受限的有界面 Chromium；可为美国戴尔单独配置上述 SOCKS5 代理，但不会代替用户完成验证码。代理路径可用也不保证站点今后始终允许自动访问；检查失败不会覆盖上次可信价格。
- 反代登录循环一般与 HTTPS/外部地址及 Cookie 有关，检查 `PRICEWATCH_EXTERNAL_URL` 和反代转发头。
- 忘记密码时，先停止服务；把密码写入权限为 600 的数据集文件，再运行 `docker compose run --rm --no-deps --entrypoint pricewatch pricewatch admin reset-password --username 用户名 --password-file /data/密码文件名`。重置后删除此明文文件，不要把密码写进命令历史。
- 用 `docker stats` 记录真实空闲内存、浏览器检查峰值；用 `docker image ls` 记录镜像大小。目前尚未测量浏览器抓取峰值，不提供未经测量的数字。

## GitHub 发布

项目以 MIT 许可证公开：[GitHub 仓库](https://github.com/shudaizi520/pricewatch)。公开的 GHCR `linux/amd64` 镜像已经按签名的 `v1.0.1` 标签发布为 `1.0.1`、`1.0`、`1` 和 `latest`；空白安装请固定 `1.0.1`，不要依赖 `latest` 回滚。发布流程使用受保护的 `production` environment 和仓库变量 `RELEASE_SIGNING_PUBLIC_KEY` 验证签名标签。本次发布私钥已删除，后续发布须生成新密钥并更新公钥。不要将 `.env` 或真实 Webhook 推上去。

CI 会完整报告镜像的高危/严重漏洞，并阻止任何已有修复版本却尚未升级的漏洞。2026-09-21 的试构建仍有 56 项 Debian 系统包告警（其中 1 项严重），扫描器尚未列出修复版本。这不是“零漏洞”；待上游发布修复后应及时重建和升级镜像。实机验收期间只应经受信任的局域网或反代访问，不应直接把 8080 暴露到公网。

依赖/许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
