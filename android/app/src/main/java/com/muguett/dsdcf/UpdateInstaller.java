package com.muguett.dsdcf;

import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HashSet;
import java.util.Set;

/** Installs only an APK signed by the same certificate as the running app. */
public final class UpdateInstaller {
    private UpdateInstaller() {
    }

    public static File validateDownloadedApk(
            Context context,
            File target,
            MarketRepository.UpdateInfo expected
    ) throws IOException {
        File directory = new File(context.getCacheDir(), "updates");
        validateExpectedMetadata(expected);
        if (!target.isFile()
                || !"DS_DCF-update.apk".equals(target.getName())
                || !directory.getCanonicalFile().equals(target.getCanonicalFile().getParentFile())) {
            throw new IOException("安装包不在应用的安全下载目录中。");
        }
        try {
            if (target.length() != expected.size || !sha256(target).equals(expected.sha256)) {
                throw new IOException("安装包内容与已验证的更新说明不一致，已拒绝安装。");
            }
            validateArchive(context, target, expected);
        } catch (IOException validationFailure) {
            // Leaving an untrusted APK in the update directory would make a
            // later accidental install possible, so remove only this file.
            if (!target.delete()) {
                target.deleteOnExit();
            }
            throw validationFailure;
        }
        return target;
    }

    public static boolean requestInstall(Context context, File apk) throws IOException {
        try {
            if (!context.getPackageManager().canRequestPackageInstalls()) {
                Intent settings = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
                        .setData(Uri.parse("package:" + context.getPackageName()));
                context.startActivity(settings);
                return false;
            }
            Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".files", apk);
            Intent install = new Intent(Intent.ACTION_VIEW)
                    .setDataAndType(uri, "application/vnd.android.package-archive")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(install);
            return true;
        } catch (ActivityNotFoundException | SecurityException | IllegalArgumentException exception) {
            throw new IOException("系统无法打开安全安装确认页面。", exception);
        }
    }

    private static void validateExpectedMetadata(MarketRepository.UpdateInfo expected) throws IOException {
        if (expected == null
                || expected.versionCode <= BuildConfig.VERSION_CODE
                || expected.versionName == null
                || expected.versionName.trim().isEmpty()
                || expected.sha256 == null
                || !expected.sha256.matches("[0-9a-f]{64}")
                || expected.signerSha256 == null
                || !MarketRepository.isExpectedReleaseSignerHash(expected.signerSha256)
                || expected.size <= 0L
                || expected.size > 50L * 1024L * 1024L) {
            throw new IOException("已保存的更新校验信息不完整，请重新检查应用更新。");
        }
    }

    private static void validateArchive(
            Context context,
            File archive,
            MarketRepository.UpdateInfo expected
    ) throws IOException {
        try {
            PackageManager manager = context.getPackageManager();
            PackageInfo installedInfo = manager.getPackageInfo(context.getPackageName(), signingFlags());
            PackageInfo candidate = manager.getPackageArchiveInfo(archive.getAbsolutePath(), signingFlags());
            if (candidate == null) {
                throw new IOException("下载文件不是有效的 Android 安装包，已拒绝安装。");
            }
            if (!context.getPackageName().equals(candidate.packageName)
                    || !context.getPackageName().equals(BuildConfig.APPLICATION_ID)) {
                throw new IOException("下载的安装包身份不匹配，已拒绝安装。");
            }
            long installedVersion = longVersionCode(installedInfo);
            long candidateVersion = longVersionCode(candidate);
            if (candidateVersion != expected.versionCode
                    || candidateVersion <= installedVersion
                    || !expected.versionName.equals(candidate.versionName)) {
                throw new IOException("下载的安装包版本与官方更新说明不一致，已拒绝安装。");
            }
            Set<String> installedCurrentSigners = currentSignerHashes(installedInfo);
            Set<String> candidateCurrentSigners = currentSignerHashes(candidate);
            Set<String> candidateHistory = signingCertificateHashes(candidate);
            if (installedCurrentSigners.isEmpty()
                    || candidateCurrentSigners.size() != 1
                    || !candidateCurrentSigners.contains(expected.signerSha256)
                    || !candidateHistory.containsAll(installedCurrentSigners)) {
                throw new IOException("下载的安装包签名与当前应用不兼容，已拒绝安装。");
            }
        } catch (PackageManager.NameNotFoundException exception) {
            throw new IOException("无法读取当前应用的签名信息。", exception);
        }
    }

    @SuppressWarnings("deprecation")
    private static long longVersionCode(PackageInfo info) {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? info.getLongVersionCode() : info.versionCode;
    }

    @SuppressWarnings("deprecation")
    private static int signingFlags() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES
                : PackageManager.GET_SIGNATURES;
    }

    @SuppressWarnings("deprecation")
    private static Set<String> signingCertificateHashes(PackageInfo info) throws IOException {
        android.content.pm.Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (info.signingInfo == null) {
                return new HashSet<>();
            }
            signatures = info.signingInfo.hasMultipleSigners()
                    ? info.signingInfo.getApkContentsSigners()
                    : info.signingInfo.getSigningCertificateHistory();
        } else {
            signatures = info.signatures;
        }
        Set<String> result = new HashSet<>();
        if (signatures == null) {
            return result;
        }
        for (android.content.pm.Signature signature : signatures) {
            result.add(sha256(signature.toByteArray()));
        }
        return result;
    }

    @SuppressWarnings("deprecation")
    private static Set<String> currentSignerHashes(PackageInfo info) throws IOException {
        android.content.pm.Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (info.signingInfo == null) {
                return new HashSet<>();
            }
            signatures = info.signingInfo.getApkContentsSigners();
        } else {
            signatures = info.signatures;
        }
        Set<String> result = new HashSet<>();
        if (signatures != null) {
            for (android.content.pm.Signature signature : signatures) {
                result.add(sha256(signature.toByteArray()));
            }
        }
        return result;
    }

    private static String sha256(byte[] value) throws IOException {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value);
            StringBuilder result = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                result.append(String.format(java.util.Locale.ROOT, "%02x", item));
            }
            return result.toString();
        } catch (NoSuchAlgorithmException exception) {
            throw new IOException("系统不支持安装包签名校验。", exception);
        }
    }

    private static String sha256(File file) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (InputStream input = new FileInputStream(file)) {
                byte[] buffer = new byte[64 * 1024];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    digest.update(buffer, 0, count);
                }
            }
            byte[] value = digest.digest();
            StringBuilder result = new StringBuilder(value.length * 2);
            for (byte item : value) {
                result.append(String.format(java.util.Locale.ROOT, "%02x", item));
            }
            return result.toString();
        } catch (NoSuchAlgorithmException exception) {
            throw new IOException("系统不支持安装包内容校验。", exception);
        }
    }
}
