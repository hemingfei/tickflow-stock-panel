# 离线部署方案

本方案适用于无法直接从云端拉取代码或构建镜像的场景，通过"本机构建 → 导出镜像 → 云端导入"的方式完成部署。

---

## 一、本机操作（构建与导出）

### 1.1 准备工作

确保本机已安装：
- Docker
- Git（用于拉取最新代码）

### 1.2 构建镜像

```bash
# 进入项目目录
cd tickflow-stock-panel

# 复制配置文件（如未配置）
cp .env.example .env
# 按需编辑 .env 文件，设置 TICKFLOW_API_KEY 等配置

# 直接构建单个镜像
docker build -t tickflow-stock-panel:latest .
```

**可选：包含 stock-sdk 插件（自行承担合规责任）**

```bash
docker build --build-arg INCLUDE_STOCKSDK=1 -t tickflow-stock-panel:latest .
```

### 1.3 导出镜像为 tar 文件

```bash
# 导出镜像（未压缩）
docker save -o tickflow-stock-panel.tar tickflow-stock-panel:latest

# 或者压缩导出（推荐，体积更小）
docker save tickflow-stock-panel:latest | gzip > tickflow-stock-panel.tar.gz
```

### 1.4 传输文件到云端

使用 scp、ftp 或其他方式将 `tickflow-stock-panel.tar`（或 `.tar.gz`）上传到服务器：

```bash
# 示例：使用 scp 上传
scp tickflow-stock-panel.tar.gz root@your-server-ip:/path/to/deploy/
```

---

## 二、云端操作（导入与运行）

### 2.1 准备工作

确保云端服务器已安装：
- Docker
- Docker Compose

### 2.2 上传文件并解压

```bash
# 进入部署目录
cd /path/to/deploy/

# 如果是压缩包，先解压
gunzip tickflow-stock-panel.tar.gz
```

### 2.3 导入镜像

```bash
# 导入镜像
docker load -i tickflow-stock-panel.tar

# 确认镜像已导入
docker images
# 应该能看到 tickflow-stock-panel:latest
```

### 2.4 准备配置文件

确保服务器上有以下文件：
- `.env`（从 `.env.example` 复制并按需配置）
- `docker-compose.yml`（项目根目录已提供）

```bash
# 如果没有配置文件，从项目复制或创建
cp .env.example .env
# 编辑 .env 设置必要配置（如 AUTH_PASSWORD 等）
```

### 2.5 启动服务

```bash
# 启动容器
docker compose up -d

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f
```

---

## 三、docker-compose.yml 参考

确保 `docker-compose.yml` 指向本地镜像：

```yaml
version: '3.8'

services:
  tickflow:
    image: tickflow-stock-panel:latest  # 使用本地导入的镜像
    container_name: tickflow-stock-panel
    ports:
      - "3018:3018"
    volumes:
      - ./data:/app/data
      - ./.env:/app/.env:ro
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
```

---

## 四、更新流程

当需要更新版本时，重复以下步骤：

**本机：**
```bash
# 拉取最新代码
git pull

# 重新构建镜像
docker build -t tickflow-stock-panel:latest .

# 导出并上传
docker save tickflow-stock-panel:latest | gzip > tickflow-stock-panel.tar.gz
scp tickflow-stock-panel.tar.gz root@your-server-ip:/path/to/deploy/
```

**云端：**
```bash
# 停止旧容器
docker compose down

# 解压并导入新镜像
gunzip tickflow-stock-panel.tar.gz
docker load -i tickflow-stock-panel.tar

# 启动新容器
docker compose up -d

# 清理旧镜像（可选）
docker image prune -f
```

---

## 五、注意事项

1. **数据持久化**：确保 `./data` 目录正确挂载，所有用户数据（自选、回测记录等）都存在这里
2. **访问密码**：公网部署时，建议在 `.env` 中预置 `AUTH_PASSWORD`，详见 [deploy-password.md](./deploy-password.md)
3. **老 CPU 兼容**：如服务器 CPU 不支持 AVX2，需在 `.env` 中设置 `BACKEND_EXTRAS=legacy-cpu`
4. **镜像清理**：定期清理旧镜像释放空间：`docker image prune -a`
5. **备份**：部署前建议备份 `./data` 目录

---

## 六、故障排查

| 问题 | 排查方法 |
|------|----------|
| 镜像导入失败 | 检查 tar 文件是否完整，MD5 校验 |
| 容器启动失败 | `docker compose logs` 查看日志 |
| 无法访问 | 检查防火墙、端口映射、容器状态 |
| 数据丢失 | 确认 volumes 挂载正确 |
