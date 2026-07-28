# DS_DCF Android 客户端

这是一个只读客户端：它只下载服务器在沪深收盘后完成全量分析并通过完整性检查的结果，不在手机上重算 DCF、评分或买入条件。

## 数据与安全边界

- 稳定数据清单固定来自项目的 GitHub Pages；清单引用的不可变压缩数据和签名仍固定来自 `mobile-market-data` 发布通道。清单使用固定在应用内的公开密钥验签，压缩目录、候选详情和签名必须属于同一个不可变批次。版本、字节数、内容摘要、交易日与解压上限全部在手机端复核。
- 下载失败、内容校验错误、完整性检查未通过或同批次字段不一致时，会保留上一份本机已校验数据。
- 活动缓存损坏时会检查仍保留的上一批已签名数据；多个界面实例同时刷新时，回滚复核与活动批次切换在同一把进程锁内完成。
- “待确认候选”“待补证据候选”和“观察名单”分别显示，绝不计入实际买入信号；待确认表示公司侧条件已满足但仍需核对真实仓位/操作，待补证据则只表示现有分数上下界仍可能达到门槛，二者不能混称。同时拥有实际信号的公司也不会从其他状态列表中消失。状态和七种买入情况可以组合筛选；“全部沪深公司”配合某一类型时，会显示该类型适用但尚未触发的公司，而不是把观察对象误显示为零家。
- 公开移动文件只保留手机会显示的分数、状态和中文解释，不包含完整估值账本或桌面审计对象。
- 检查更新固定读取 `android-app` 发布通道的公开更新说明及其独立 P-256 签名；客户端先验签、再完整校验清单，最后才比较版本。手机会保存已见最高 `versionCode`，拒绝旧清单回放。安装包会边下载边写入应用私有目录并核对文件完整性，安装前还必须与当前应用具有兼容的签名证书。Android 系统仍会要求用户确认安装，应用不会静默更新。

## 本地构建

安装 Java 17 和 Android SDK（`platforms;android-35`、`build-tools;35.0.0`）后：

```powershell
$env:JAVA_HOME = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot'
$env:ANDROID_SDK_ROOT = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat --no-daemon :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
```

Release APK 必须使用一个持久、私有且从不提交到仓库的签名密钥：

```powershell
$env:DS_DCF_ANDROID_KEYSTORE = 'C:\safe\ds-dcf-release.jks'
$env:DS_DCF_ANDROID_STORE_PASSWORD = '<private>'
$env:DS_DCF_ANDROID_KEY_ALIAS = 'ds-dcf-release'
$env:DS_DCF_ANDROID_KEY_PASSWORD = '<private>'
.\gradlew.bat --no-daemon :app:assembleRelease
```

发布前给 APK 一个版本化文件名。必须在仓库根目录、已提交且包含未跟踪文件在内完全干净的 Git 工作树中生成更新清单；工具会确认当前仓库根目录和 `HEAD` 提交对象。下面把产物放入已被 Git 忽略的 `dist/`，不要先把 APK 放进未忽略的仓库路径。然后再使用与 APK 证书、每日市场数据和桌面更新都不同的 P-256 私钥签署公开清单：

```powershell
$releaseDir = 'dist\official-v11.3.2\android'
New-Item -ItemType Directory -Force $releaseDir | Out-Null
Copy-Item android\app\build\outputs\apk\release\app-release.apk `
  "$releaseDir\DS_DCF-v11.3.2-android-release.apk"
python -m tools.android_release `
  --apk "$releaseDir\DS_DCF-v11.3.2-android-release.apk" `
  --version-code 3 `
  --version-name 11.3.2 `
  --release-tag v11.3.2 `
  --output "$releaseDir\android-update-manifest.json" `
  --provenance-output "$releaseDir\android-release-provenance.json"
$env:DS_DCF_ANDROID_UPDATE_SIGNING_PRIVATE_KEY_BASE64 = '<从仓库外安全存储载入>'
try {
  pwsh -NoProfile -File tools\sign_android_update_manifest.ps1 `
    -Manifest "$releaseDir\android-update-manifest.json" `
    -Output "$releaseDir\android-update-manifest.sig"
} finally {
  Remove-Item Env:DS_DCF_ANDROID_UPDATE_SIGNING_PRIVATE_KEY_BASE64 -ErrorAction SilentlyContinue
}
```

正式清单生成成功时，工具会在控制台报告精确的 Git 提交，并把该提交、安装包摘要和公开清单摘要写入本地 `android-release-provenance.json`（未指定参数时也会默认写到公开清单旁边）。这个旁路文件应随本地正式构建档案保存，但不要替代或加入 `android-app` 固定更新通道。为了让已经安装的 11.2 客户端仍能读取新版更新说明，`git_sha` 不会加入字段集合固定的 `android-update-manifest.json`；普通 `assembleDebug`、debug 单元测试和 lint 不经过这项正式发布门禁。

版本化 APK 上传到对应的正式版本 Release；`android-update-manifest.json` 与 `android-update-manifest.sig` 必须一起发布到固定的 `android-app` Release，已安装客户端才不会因其他平台后来发布新版本而丢失更新入口。首个 APK 可以手动安装；以后版本须保留同一个 APK 签名证书和 Android 更新清单私钥，并严格递增 `versionCode`。
