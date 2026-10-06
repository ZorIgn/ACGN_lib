# ACGLib 容器与安装密钥

两份 Compose 均 `build: .` 构建本仓库，使用 `config.container_settings`：ACGLib 书架界面、持久 Redis broker 和服务器 worker。桌面 `config.native_settings` 保持单进程内存队列。默认发布端口绑定 127.0.0.1；准备好身份配置后再调整对外监听。

SQLite 新安装未提供 `SECRET`/`SECRET_FILE` 时，自动创建独立持久密钥：源码默认 `src/db/.app-secret`，Compose 明确设置 `/yamtrack/db/.app-secret` 并挂载持久数据目录。Postgres Compose 同样为应用密钥挂载 `app_data` 卷，数据库密码须通过 `DB_PASSWORD` 提供；外部数据库必须显式提供私有 `SECRET`，避免旧库被误配新钥。并发首次初始化使用原子发布，权限为 0600。镜像不包含本地安装密钥。

已有私有 `SECRET` 保持不变。数据库已经存在但密钥配置/密钥文件缺失时拒绝自动生成，必须先恢复旧钥并处理迁移；原生启动器也复用持久密钥，不会因 `.env` 丢失另换新钥。已知公开占位密钥会拒绝启动，不能直接换钥，否则已有导入凭据不能解密。升级此类旧安装时，应先备份数据库与旧密钥，在受控环境解密并用新钥重新加密凭据，或移除旧导入凭据后重新录入，再切换应用签名密钥。备份/迁移应把持久密钥与数据库一起保管。

任务结果格式常量移入 `integrations.contracts`，页面不再依赖 Celery task 模块的隐式导入顺序。

回归需区分运行模式：通用用户任务测试使用 `config.test_settings`；书架测试使用 `config.native_settings`；容器界面、任务结果及密钥测试使用 `config.container_settings` 和可用 Redis。容器检查命令：`docker compose config --quiet`、`DJANGO_SETTINGS_MODULE=config.container_settings python src/manage.py check`。
