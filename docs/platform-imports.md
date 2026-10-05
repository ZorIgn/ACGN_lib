# 平台记录导入

## Bangumi

在「作品数据 → Bangumi 导入」填写个人主页 `/user/` 后的用户名或数字 ID，点击「获取收藏并预览」。确认新增、已存在与跳过数量，展开清单检查作品和记录，再确认导入。预览在当前登录账户下保留 30 分钟。

公开收藏通过 Bangumi 官方 API 分页读取，无需密码或访问令牌。读取范围包括所有收藏状态：

| Bangumi 数据 | ACGLib 记录 |
| --- | --- |
| 书籍 | 根据条目资料区分小说、漫画 |
| 动画、游戏、三次元 | 动画、游戏、剧集 |
| 想看、看过、在看、搁置、抛弃 | 想看 / 想玩、已完成、进行中、暂停、已放弃 |
| 1–10 分、0 | 对应评分、未评分 |
| 吐槽 | 我的记录，保留原文 |
| 已读卷 / 章 / 话数、已看集数 | 阅读 / 观看位置 |

预览完整读取后才允许导入；网络失败可以重新获取。已有同来源、同类型、同条目 ID 的作品保留当前评分、状态、感想、位置与文件夹。重复记录只新增一次。音乐、私有收藏与资料不可访问的条目不导入，公开列表中被跳过的条目会显示数量。Bangumi 不提供累计游戏时长，导入游戏的累计时长为 0。

单次读取上限为 10000 条公开收藏、5 MiB 作品数据。Bangumi 已读集数表示完成数量，未必等于最后观看集的序号。

接口依据：[Bangumi 官方 API](https://bangumi.github.io/api/)、[官方 OpenAPI 定义](https://github.com/bangumi/api/blob/master/open-api/v0.yaml)。集合端点使用 `limit=50` 与 `offset`；书籍摘要缺少分类时读取条目详情。

## PlayStation

截至 2026-09-26，能够核实的官方接入渠道是 [PlayStation Partners](https://partners.playstation.net/)。[Sony Interactive Entertainment 的开发者说明](https://sonyinteractive.com/en/news/blog/showing-your-game-to-playstation/)指出，注册获批并签署 GDPA 后可取得开发工具和文档。

公开官方资料尚不能确认一条面向个人书架、可自行注册客户端并经用户授权读取 PSN 游戏库与游玩时长的 API 路径。因此当前可直接使用的是 Steam 同步和 Bangumi 公开收藏导入；PlayStation 自动同步需先获得适用的官方接入权限与接口文档，再验证授权范围和数据字段。
