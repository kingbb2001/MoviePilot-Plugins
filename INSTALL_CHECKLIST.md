# Emby质量监控插件 - 安装检查清单

## 一、GitHub 仓库文件检查

请访问以下链接，确认文件存在且可访问：

### 1.1 package.json（必须）
```
https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/package.json
```
确认包含：
```json
"EmbyQualityMonitor": {
  "name": "Emby质量监控",
  "version": "1.0.0",
  ...
}
```

### 1.2 插件主文件（必须）
```
https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/plugins/embyqualitymonitor/__init__.py
```

### 1.3 插件逻辑文件（必须）
```
https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/plugins/embyqualitymonitor/main.py
```

### 1.4 图标文件（可选）
```
https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main/icons/emby_quality_monitor.svg
```

---

## 二、命名规范检查

| 位置 | 命名 | 说明 |
|------|------|------|
| 目录名 | `embyqualitymonitor` | 全小写，无下划线 |
| 类名 | `EmbyQualityMonitor` | 驼峰命名（首字母大写）|
| package.json key | `EmbyQualityMonitor` | 与类名一致 |
| 显示名称 | `Emby质量监控` | 可以包含中文 |

**状态：✅ 命名规范正确**

---

## 三、MoviePilot 配置检查

### 3.1 插件仓库配置

**路径：** 设置 → 系统设置 → 插件仓库

**必须添加：**
```
https://github.com/kingbb2001/MoviePilot-Plugins
```

或

```
https://raw.githubusercontent.com/kingbb2001/MoviePilot-Plugins/main
```

### 3.2 刷新插件列表

**路径：** 设置 → 插件 → 右上角刷新按钮

**操作步骤：**
1. 点击刷新按钮
2. 等待10-20秒
3. 在搜索框输入：`Emby质量监控`

---

## 四、常见问题排查

### 问题1：插件市场看不到插件

**原因：** MP 缓存了旧的插件列表

**解决方案：**
```bash
# 方法1：重启 MP 容器
docker restart moviepilot

# 方法2：查看日志
docker logs moviepilot 2>&1 | grep -i "plugin"
```

### 问题2：插件仓库配置失败

**原因：** 网络问题或仓库地址错误

**解决方案：**
1. 检查网络连接
2. 确认仓库地址正确
3. 尝试使用 raw.githubusercontent.com 地址

### 问题3：插件安装失败

**原因：** 文件权限或目录结构问题

**解决方案：**
```bash
# SSH 登录到飞牛OS
ssh kalax@你的NAS地址

# 检查插件目录权限
ls -la /vol1/1000/docker/AppData/moviepilot/plugins/

# 如果权限不对，修复权限
chmod -R 755 /vol1/1000/docker/AppData/moviepilot/plugins/
```

---

## 五、手动安装方法（如果插件市场不可用）

### 5.1 下载插件文件

```bash
# SSH 登录到飞牛OS
ssh kalax@你的NAS地址

# 进入 MP 插件目录
cd /vol1/1000/docker/AppData/moviepilot/plugins/

# 下载插件仓库
git clone --depth=1 https://github.com/kingbb2001/MoviePilot-Plugins.git temp

# 复制插件到当前目录
cp -r temp/plugins/embyqualitymonitor ./

# 清理临时文件
rm -rf temp
```

### 5.2 重启 MP

```bash
docker restart moviepilot
```

### 5.3 验证插件加载

```bash
# 查看 MP 日志
docker logs moviepilot 2>&1 | grep -i "embyqualitymonitor\|emby.*quality"
```

应该能看到类似：
```
[INFO] Loading plugin: EmbyQualityMonitor
[INFO] Plugin EmbyQualityMonitor loaded successfully
```

---

## 六、插件配置步骤

### 6.1 启用插件

1. 进入 **设置 → 插件**
2. 找到 **Emby质量监控**
3. 点击进入配置页面

### 6.2 配置参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| Emby服务器 | 选择已配置的服务 | 从MP媒体服务器配置中选择 |
| 媒体库名称 | `电影` | 要监控的Emby媒体库名称 |
| 最低分辨率 | `1080p` | 低于此值将被标记 |
| 优先编码 | `h265,hevc,av1` | 不在此列表将被标记 |
| 最低来源 | `BluRay` | 低于此值将被标记 |
| 要求HDR | 关闭 | 开启后SDR将被标记 |
| 自动删除旧版本 | 开启 | 需配合MP整理覆盖模式 |
| 定时扫描 | `0 2 * * *` | 每天凌晨2点 |
| 开启通知 | 开启 | 扫描完成后推送 |

### 6.3 重要：配置整理覆盖模式

**路径：** 设置 → 目录 → 整理模式 → 覆盖模式

**选项：**
- `仅保留最新版本` = 自动删除旧版本
- `从不覆盖` = 新旧版本共存

---

## 七、测试插件功能

### 7.1 手动触发扫描

1. 在插件配置页面勾选 **"立即运行一次"**
2. 保存配置
3. 查看通知或日志

### 7.2 通过API测试

```bash
# 扫描媒体库
curl -X GET "http://localhost:3001/api/v1/plugin/EmbyQualityMonitor/scan" \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# 批量创建订阅
curl -X POST "http://localhost:3001/api/v1/plugin/EmbyQualityMonitor/subscribe" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "movies": [
      {"title": "沙丘2", "year": "2024", "tmdb_id": "823464"}
    ]
  }'
```

---

## 八、获取帮助

如果以上步骤都无法解决问题，请提供：

1. **MP 日志**
   ```bash
   docker logs moviepilot --tail 100
   ```

2. **插件列表截图**
   - 设置 → 插件 → 截图

3. **插件仓库配置截图**
   - 设置 → 系统设置 → 插件仓库 → 截图

4. **网络测试结果**
   - 能否访问 GitHub
   - 能否访问 raw.githubusercontent.com

---

**更新日期：** 2026-04-13
**插件版本：** v1.0.0
**仓库地址：** https://github.com/kingbb2001/MoviePilot-Plugins
