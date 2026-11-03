# 发版与 PyPI 发布

## 当前链路

仅当推送形如 `v1.0.113` 的 tag 时触发（`.github/workflows/publish.yml`）：

1. **validate**：校验 tag 是严格 `vX.Y.Z`，且去掉 `v` 后与 `tg_signer/__init__.py` 的 `__version__` 完全一致；不一致直接失败。
2. **test**：复用 `.github/workflows/test.yml`，跑 `ruff check` 与 Python 3.10/3.11/3.12 的 pytest。
3. **build**：清空 `dist/`、`build/` 后构建，只把本次的 wheel/sdist 作为 artifact 上传。
4. **publish**：下载本次 artifact，通过 PyPI Trusted Publishing（OIDC）发布，不再使用长期 token。

版本唯一来源是 `tg_signer/__init__.py` 的 `__version__`；`pyproject.toml` 使用 `dynamic = ["version"]` 读取同一个值。

Docker 镜像（`.github/workflows/docker-publish.yml`）同样只响应严格 `vX.Y.Z` tag。`latest` / `latest-webui` 只在“该 tag 是当前最高的稳定版本，且提交在默认分支上”时更新。

## 正常发版步骤

1. 修改 `tg_signer/__init__.py` 的 `__version__`（例如 `1.0.113`）。
2. 提交并推送 `main`。
3. 创建并推送 tag：`git tag v1.0.113 && git push origin v1.0.113`。
4. 在 GitHub Actions 中确认 `Publish Python Package` 的 validate → test → build → publish 全部通过。

不要同时用本地 `scripts/release_pypi.py` 上传并推送同一个 tag：本地已经上传成功后，CI 再发布同一版本会因版本已存在而失败。该脚本仅作为 Actions 不可用时的应急通道。

## 一次性配置（上线前必须做）

### 1. 恢复 GitHub Actions 调度

当前现象是 tag 已到达 GitHub，但自 2026-10-14 起没有任何 workflow 被调度。代码无法修复账户/仓库级开关，需要人工检查：

- 仓库 **Settings → Actions → General**：确认没有选择 “Disable actions”，并允许使用本地 reusable workflow（例如 “Allow all actions and reusable workflows”，或至少允许 `./.github/workflows/*`）。
- 仓库 **Actions** 页面：如果出现 Actions 被禁用/暂停的提示，按提示重新启用。
- 个人/组织 **Billing & plans**：确认 Actions 没有被账单或 spending limit 暂停。公开仓库通常不产生 Actions 分钟数费用，但仍需确认账户状态。
- 若仓库属于组织，检查组织级 Actions 策略是否禁止了该仓库或第三方 action。

恢复后，任意一次 push（例如推送一个新 tag）应立刻在 Actions 页面出现新的 run。

### 2. 配置 PyPI Trusted Publisher

在 PyPI 项目 `tg-signer-plus` 的 **Settings → Publishing** 中添加 GitHub Trusted Publisher，字段必须与 workflow 完全一致：

| 字段 | 值 |
| --- | --- |
| Owner | `zhangyunihao` |
| Repository | `tg-signer` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

### 3. 配置 GitHub environment

在仓库 **Settings → Environments** 新建名为 `pypi` 的 environment：

- 可添加 Required reviewers（建议至少 1 人），这样真正上传前需要人工批准。
- Deployment branches and tags 建议限制为 `v*.*.*`，避免其他 ref 使用该 environment。
- 不需要在该 environment 中放任何 token（Trusted Publishing 走 OIDC）。

### 4. 清理旧 token

首次 Trusted Publishing 成功后再删除旧 secret `PYPI_PASSWORD`：

- 若它配置在仓库级：**Settings → Secrets and variables → Actions → Secrets**。
- 若它配置在 environment 级：**Settings → Environments → pypi → Environment secrets**。
- 同时在 PyPI 账户中吊销对应的旧 API token，避免残留凭据。

## 首次验证

1. 先确认 Actions 已恢复（见上文）。
2. 选一个尚未发布过的版本号，例如 `1.0.113`，确认 `tg_signer/__init__.py` 已是该版本。
3. 推送 `v1.0.113` tag，观察 validate/test/build/publish 四段是否全部成功。
4. 验证失败时不要复用该版本号重推：PyPI 不接受重复版本。修复后升到下一个未使用的 patch 版本再发。

## 回退方案：改用 API token

仅当 Trusted Publishing 因 PyPI 侧配置问题暂时不可用时：

1. 在 PyPI 创建**项目级** API token（Scope 只选 `tg-signer-plus`）。
2. 在 GitHub 仓库 **Settings → Environments → pypi → Environment secrets** 新建 `PYPI_API_TOKEN`；或仍在仓库级 Secrets 中创建同名 secret。
3. 把 `publish.yml` 中的 `pypa/gh-action-pypi-publish` 步骤替换为：

   ```yaml
   - name: Publish to PyPI (token fallback)
     env:
       TWINE_USERNAME: __token__
       TWINE_PASSWORD: ${{ secrets.PYPI_API_TOKEN }}
     run: python -m pip install twine && python -m twine upload --skip-existing dist/*
   ```

4. 回退时可以移除 `id-token: write`，但保留 `environment: pypi` 时 secret 必须建在该 environment 下。
5. 恢复 Trusted Publishing 后删除 `PYPI_API_TOKEN` 并吊销对应 PyPI token。

## 已有 tag 的影响

- 修改触发条件不会重跑历史 tag，已发布的 PyPI 版本与已有 GHCR 镜像保持不变。
- 历史无 `v` 前缀的 tag（如 `1.0.13`、`1.0.31`）不再触发发布；即使重推也不会触发。
- 已有的 `v1.0.32`～`v1.0.112` 仍匹配新规则。若删除后重推旧 tag：
  - PyPI 会因版本已存在而拒绝，发布失败但不会覆盖线上版本；
  - Docker 会重建该版本镜像，但由于它通常不是最高版本，不会移动 `latest`。
- `v1.0.113` 及之后的新 tag 是第一次按新链路完整验证的版本。
