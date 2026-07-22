package com.muguett.dsdcf;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.zip.GZIPInputStream;

/**
 * Read-only client for the post-close, quality-gated market snapshot.
 *
 * The phone never calculates a valuation.  It accepts a catalogue only after
 * checking the download size, SHA-256 and schema published by the server.
 */
public final class MarketRepository {
    public static final String MOBILE_RELEASE_ASSET_BASE =
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data/";
    public static final String MOBILE_MANIFEST_URL =
            "https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json";
    public static final String UPDATE_MANIFEST_URL =
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.json";
    public static final String UPDATE_MANIFEST_SIGNATURE_URL =
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.sig";

    private static final int SNAPSHOT_SCHEMA_VERSION = 1;
    private static final int MAX_MANIFEST_BYTES = 1_000_000;
    private static final int MAX_MANIFEST_SIGNATURE_BYTES = 1_024;
    private static final int MAX_COMPRESSED_ASSET_BYTES = 8_000_000;
    private static final int MAX_UNCOMPRESSED_ASSET_BYTES = 16_000_000;
    private static final int MAX_UPDATE_MANIFEST_BYTES = 1_000_000;
    private static final int MAX_PUBLIC_REASON_LENGTH = 200;
    private static final long MAX_APK_BYTES = 50L * 1024L * 1024L;
    private static final int MIN_SH_SZ_COMPANY_COUNT = 4_500;
    private static final int MAX_SH_SZ_COMPANY_COUNT = 6_500;
    private static final Duration MAX_SNAPSHOT_AGE = Duration.ofDays(14);
    private static final Duration MAX_FUTURE_CLOCK_SKEW = Duration.ofMinutes(10);
    private static final Object CACHE_COMMIT_LOCK = new Object();
    private static final String MOBILE_SIGNING_PUBLIC_KEY_BASE64 =
            "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAExQ3XrBYfIsZilmdQvnTqIcqo7mCPhRTOnntpt/hqA+mCkHaHRGhjEyd3ek5XNRyjhadmMl364s8MBOjAySPENg==";
    private static final String ANDROID_UPDATE_SIGNING_PUBLIC_KEY_BASE64 =
            "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjic3+c4snSCoVhcipasA9t3ppCwvRO5u88dg/M1oul+Y3Wp0BwR/Z9bq9ywZK3NgDn7SH3pluAU3MOdQqcVoIA==";
    private static final String RELEASE_CERT_SHA256 =
            "e818fa2a0d18b12316e826bdaeb1877a62ccb68634b42fdd598c687a74293369";
    private static final Set<String> UPDATE_MANIFEST_FIELDS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "apk_sha256",
            "apk_size",
            "apk_url",
            "package_id",
            "schema_version",
            "signer_sha256",
            "version_code",
            "version_name"
    )));
    private static final Set<String> TRUSTED_DOWNLOAD_HOSTS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
            "github-releases.githubusercontent.com",
            "muguett-dby.github.io"
    )));
    private static final Set<String> VALID_TYPE_STATUSES = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "triggered",
            "conditional",
            "observe",
            "insufficient_evidence",
            "vetoed",
            "blocked",
            "not_triggered",
            "not_applicable"
    )));

    private final File cacheDirectory;
    private final File updateVersionWatermarkFile;

    public MarketRepository(Context context) {
        cacheDirectory = new File(context.getFilesDir(), "market-snapshot");
        updateVersionWatermarkFile = new File(context.getFilesDir(), "android-update-version-watermark");
    }

    public MarketData loadCached() throws IOException, JSONException {
        synchronized (CACHE_COMMIT_LOCK) {
            return loadBestCachedGeneration(true);
        }
    }

    private MarketData loadBestCachedGeneration(boolean repairActivePointer) throws IOException, JSONException {
        File activePointer = new File(cacheDirectory, "active-generation");
        String activeGeneration = "";
        if (activePointer.isFile()) {
            try {
                String candidate = new String(readFileLimited(activePointer, 128), StandardCharsets.US_ASCII).trim();
                if (candidate.matches("[0-9a-f]{64}")) {
                    activeGeneration = candidate;
                }
            } catch (IOException ignored) {
                // A damaged pointer must not hide an intact signed fallback.
            }
        }
        File generationsDirectory = new File(cacheDirectory, "generations");
        File[] directories = generationsDirectory.listFiles(
                file -> file.isDirectory() && file.getName().matches("[0-9a-f]{64}")
        );
        List<File> candidates = new ArrayList<>();
        if (!activeGeneration.isEmpty()) {
            candidates.add(new File(generationsDirectory, activeGeneration));
        }
        if (directories != null) {
            Arrays.sort(directories, (left, right) -> Long.compare(right.lastModified(), left.lastModified()));
            for (File directory : directories) {
                if (!directory.getName().equals(activeGeneration)) {
                    candidates.add(directory);
                }
            }
        }
        MarketData best = null;
        String bestGeneration = "";
        SnapshotWatermark bestWatermark = null;
        Exception lastFailure = null;
        for (File candidate : candidates) {
            MarketData parsed;
            try {
                parsed = loadGeneration(candidate);
            } catch (IOException | JSONException failure) {
                lastFailure = failure;
                continue;
            }
            SnapshotWatermark candidateWatermark = new SnapshotWatermark(
                    parsed.marketDate,
                    parsed.dataTimestamp,
                    candidate.getName()
            );
            SnapshotWatermark selected = selectSnapshotWatermark(bestWatermark, candidateWatermark);
            if (selected == candidateWatermark) {
                best = parsed;
                bestGeneration = candidate.getName();
                bestWatermark = candidateWatermark;
            }
        }
        if (best == null) {
            if (candidates.isEmpty()) {
                throw new IOException("手机中还没有已校验的市场数据，请点击“获取最新数据”。");
            }
            throw new IOException("手机中已保存的市场数据已损坏，请重新获取最新数据。", lastFailure);
        }
        if (repairActivePointer && !bestGeneration.equals(activeGeneration)) {
            writeAtomically(
                    activePointer,
                    (bestGeneration + "\n").getBytes(StandardCharsets.US_ASCII)
            );
        }
        SnapshotWatermark persistedWatermark = readSnapshotWatermark(
                new File(cacheDirectory, "accepted-watermark")
        );
        SnapshotWatermark acceptedWatermark = selectSnapshotWatermark(persistedWatermark, bestWatermark);
        if (acceptedWatermark != bestWatermark) {
            throw new IOException("手机中只剩下早于已验证水位的市场数据，已拒绝回退。");
        }
        if (persistedWatermark == null || acceptedWatermark != persistedWatermark) {
            writeSnapshotWatermark(new File(cacheDirectory, "accepted-watermark"), acceptedWatermark);
        }
        return best;
    }

    private MarketData loadGeneration(File generationDirectory) throws IOException, JSONException {
        try {
            return loadGenerationChecked(generationDirectory);
        } catch (OutOfMemoryError memoryFailure) {
            throw new IOException("本机内存不足，无法安全打开这批市场数据。", memoryFailure);
        }
    }

    private MarketData loadGenerationChecked(File generationDirectory) throws IOException, JSONException {
        File manifestFile = new File(generationDirectory, "manifest.json");
        File signatureFile = new File(generationDirectory, "manifest.sig");
        File catalogueFile = new File(generationDirectory, "catalog.json.gz");
        File signalsFile = new File(generationDirectory, "signals.json.gz");
        if (!manifestFile.isFile() || !signatureFile.isFile() || !catalogueFile.isFile() || !signalsFile.isFile()) {
            throw new IOException("手机中的市场数据不完整，请重新获取最新数据。");
        }
        byte[] manifestBytes = readFileLimited(manifestFile, MAX_MANIFEST_BYTES);
        byte[] signatureBytes = readFileLimited(signatureFile, MAX_MANIFEST_SIGNATURE_BYTES);
        byte[] catalogueBytes = readFileLimited(catalogueFile, MAX_COMPRESSED_ASSET_BYTES);
        byte[] signalsBytes = readFileLimited(signalsFile, MAX_COMPRESSED_ASSET_BYTES);
        if (!generationDirectory.getName().equals(sha256(manifestBytes))) {
            throw new IOException("手机中的市场数据目录与清单内容不匹配。");
        }
        verifyManifestSignature(manifestBytes, signatureBytes);
        return parseMarketDataSafely(manifestBytes, catalogueBytes, signalsBytes);
    }

    public MarketData refresh() throws IOException, JSONException {
        byte[] manifestBytes = download(MOBILE_MANIFEST_URL, MAX_MANIFEST_BYTES);
        JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
        String signatureFilename = signatureFilename(manifest);
        byte[] signatureBytes = download(
                MOBILE_RELEASE_ASSET_BASE + signatureFilename,
                MAX_MANIFEST_SIGNATURE_BYTES
        );
        verifyManifestSignature(manifestBytes, signatureBytes);
        AssetMeta catalogueMeta = catalogueMeta(manifest);
        AssetMeta signalsMeta = signalsMeta(manifest);
        validateGenerationFilenames(signatureFilename, catalogueMeta.filename, signalsMeta.filename);
        byte[] catalogueBytes = download(MOBILE_RELEASE_ASSET_BASE + catalogueMeta.filename, MAX_COMPRESSED_ASSET_BYTES);
        byte[] signalsBytes = download(MOBILE_RELEASE_ASSET_BASE + signalsMeta.filename, MAX_COMPRESSED_ASSET_BYTES);
        verifyAsset(catalogueBytes, catalogueMeta);
        verifyAsset(signalsBytes, signalsMeta);
        MarketData data = parseMarketDataSafely(manifestBytes, catalogueBytes, signalsBytes);
        SnapshotWatermark incomingWatermark = new SnapshotWatermark(
                data.marketDate,
                data.dataTimestamp,
                sha256(manifestBytes)
        );
        synchronized (CACHE_COMMIT_LOCK) {
            try {
                loadBestCachedGeneration(true);
            } catch (SnapshotContentConflictException conflict) {
                throw conflict;
            } catch (IOException | JSONException ignored) {
                // A missing or fully corrupt old generation must not prevent a
                // newly verified generation from repairing the local cache.
            }
            File watermarkFile = new File(cacheDirectory, "accepted-watermark");
            SnapshotWatermark acceptedWatermark = readSnapshotWatermark(watermarkFile);
            SnapshotWatermark selectedWatermark = selectSnapshotWatermark(acceptedWatermark, incomingWatermark);
            if (selectedWatermark != incomingWatermark) {
                throw new IOException("服务器返回的数据早于手机中已保存的数据，已拒绝回退。");
            }
            persistGeneration(manifestBytes, signatureBytes, catalogueBytes, signalsBytes);
            if (acceptedWatermark == null || selectedWatermark != acceptedWatermark) {
                writeSnapshotWatermark(watermarkFile, selectedWatermark);
            }
        }
        return data;
    }

    public UpdateInfo checkForUpdate() throws IOException, JSONException {
        byte[] manifestBytes = download(UPDATE_MANIFEST_URL, MAX_UPDATE_MANIFEST_BYTES);
        byte[] signatureBytes;
        try {
            signatureBytes = download(UPDATE_MANIFEST_SIGNATURE_URL, MAX_MANIFEST_SIGNATURE_BYTES);
        } catch (IOException unavailableSignature) {
            throw new IOException("应用更新签名文件不可用，已拒绝把这份更新说明当作最新版。");
        }
        UpdateInfo update = parseSignedUpdateManifest(
                manifestBytes,
                signatureBytes,
                ANDROID_UPDATE_SIGNING_PUBLIC_KEY_BASE64
        );
        String manifestSha256 = sha256(manifestBytes);
        synchronized (CACHE_COMMIT_LOCK) {
            acceptUpdateManifestWatermark(
                    updateVersionWatermarkFile,
                    BuildConfig.VERSION_CODE,
                    update.versionCode,
                    update.versionName,
                    manifestSha256
            );
        }
        return update.versionCode <= BuildConfig.VERSION_CODE ? null : update;
    }

    public static File downloadApk(Context context, UpdateInfo update) throws IOException {
        File directory = new File(context.getCacheDir(), "updates");
        if (!directory.isDirectory() && !directory.mkdirs()) {
            throw new IOException("无法创建更新下载目录。");
        }
        File target = new File(directory, "DS_DCF-update.apk");
        downloadVerifiedFile(update.url, update.size, update.sha256, target);
        return target;
    }

    static UpdateInfo parseSignedUpdateManifest(
            byte[] manifestBytes,
            byte[] signatureBytes,
            String publicKeyBase64
    ) throws IOException, JSONException {
        verifyDetachedSignature(
                manifestBytes,
                signatureBytes,
                publicKeyBase64,
                "应用更新签名校验失败，已拒绝使用这份更新说明。"
        );
        JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
        Set<String> fields = new HashSet<>();
        Iterator<String> keys = manifest.keys();
        while (keys.hasNext()) {
            fields.add(keys.next());
        }
        if (!UPDATE_MANIFEST_FIELDS.equals(fields)) {
            throw new IOException("官方更新说明的字段不完整或包含未知字段。");
        }
        Object schemaValue = manifest.get("schema_version");
        Object versionCodeValue = manifest.get("version_code");
        Object apkSizeValue = manifest.get("apk_size");
        if (!isJsonInteger(schemaValue)
                || ((Number) schemaValue).longValue() != 1L
                || !isJsonInteger(versionCodeValue)
                || !isJsonInteger(apkSizeValue)) {
            throw new IOException("官方更新说明的数字字段格式不合法。");
        }
        long versionCode = ((Number) versionCodeValue).longValue();
        long size = ((Number) apkSizeValue).longValue();
        String packageId = requireJsonString(manifest, "package_id");
        String versionName = requireJsonString(manifest, "version_name");
        String url = requireJsonString(manifest, "apk_url");
        String apkSha256 = requireJsonString(manifest, "apk_sha256");
        String signerSha256 = requireJsonString(manifest, "signer_sha256");
        if (!BuildConfig.APPLICATION_ID.equals(packageId)) {
            throw new IOException("更新清单的软件包标识不匹配。");
        }
        if (versionCode <= 0L || versionCode > Integer.MAX_VALUE) {
            throw new IOException("官方更新说明的版本编号不合法。");
        }
        if (!versionName.matches("[0-9]+(?:\\.[0-9]+){2}(?:[-.][A-Za-z0-9]+)*")
                || versionName.length() > 64) {
            throw new IOException("官方更新说明的版本名称不合法。");
        }
        String releasePrefix = "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/v"
                + versionName + "/";
        String assetName = url.startsWith(releasePrefix) ? url.substring(releasePrefix.length()) : "";
        if (!isTrustedDownloadUrl(url)
                || !url.equals(releasePrefix + assetName)
                || !assetName.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,199}\\.apk")) {
            throw new IOException("官方更新说明的安装包地址不合法。");
        }
        if (!apkSha256.matches("[0-9a-f]{64}")) {
            throw new IOException("官方更新说明的安装包内容校验值不合法。");
        }
        if (!isExpectedReleaseSignerHash(signerSha256)) {
            throw new IOException("官方更新说明的安装包签名身份不匹配。");
        }
        if (size <= 0L || size > MAX_APK_BYTES) {
            throw new IOException("官方更新说明的安装包大小不合法。");
        }
        String canonical = "{\"apk_sha256\":\"" + apkSha256
                + "\",\"apk_size\":" + size
                + ",\"apk_url\":\"" + url
                + "\",\"package_id\":\"" + packageId
                + "\",\"schema_version\":1"
                + ",\"signer_sha256\":\"" + signerSha256
                + "\",\"version_code\":" + versionCode
                + ",\"version_name\":\"" + versionName + "\"}\n";
        if (!Arrays.equals(manifestBytes, canonical.getBytes(StandardCharsets.UTF_8))) {
            throw new IOException("官方更新说明不是规范格式，已拒绝使用。");
        }
        return new UpdateInfo(versionCode, versionName, url, apkSha256, signerSha256, size);
    }

    private static boolean isJsonInteger(Object value) {
        return value instanceof Integer || value instanceof Long;
    }

    private static String requireJsonString(JSONObject value, String key) throws IOException, JSONException {
        Object raw = value.get(key);
        if (!(raw instanceof String)) {
            throw new IOException("官方更新说明的文本字段格式不合法。");
        }
        String result = (String) raw;
        if (result.isEmpty() || !result.equals(result.trim())) {
            throw new IOException("官方更新说明的文本字段格式不合法。");
        }
        return result;
    }

    static UpdateManifestWatermark acceptUpdateManifestWatermark(
            File watermarkFile,
            long installedVersionCode,
            long candidateVersionCode,
            String candidateVersionName,
            String candidateManifestSha256
    ) throws IOException {
        if (installedVersionCode <= 0L
                || installedVersionCode > Integer.MAX_VALUE
                || candidateVersionCode <= 0L
                || candidateVersionCode > Integer.MAX_VALUE) {
            throw new IOException("应用更新版本水位不合法。");
        }
        UpdateManifestWatermark candidate = new UpdateManifestWatermark(
                candidateVersionCode,
                candidateVersionName,
                candidateManifestSha256
        );
        candidate.validate();
        UpdateManifestWatermark stored = readUpdateManifestWatermark(watermarkFile);
        if (candidateVersionCode < installedVersionCode
                || (stored != null && candidateVersionCode < stored.versionCode)) {
            throw new IOException("服务器返回了早于手机曾经验证过的应用版本，已拒绝回退。");
        }
        if (stored != null && candidateVersionCode == stored.versionCode) {
            if (!candidateVersionName.equals(stored.versionName)
                    || !candidateManifestSha256.equals(stored.manifestSha256)) {
                throw new IOException("同一应用版本出现了不同的已签名更新说明，已拒绝替换历史水位。");
            }
            return stored;
        }
        writeUpdateManifestWatermark(watermarkFile, candidate);
        return candidate;
    }

    static UpdateManifestWatermark readUpdateManifestWatermark(File watermarkFile) throws IOException {
        if (!watermarkFile.exists()) {
            return null;
        }
        if (!watermarkFile.isFile()) {
            throw new IOException("应用更新清单水位已损坏，无法安全判断是否为最新版本。");
        }
        String payload = new String(readFileLimited(watermarkFile, 256), StandardCharsets.US_ASCII);
        String[] lines = payload.split("\\n", -1);
        if (lines.length != 4 || !lines[3].isEmpty() || !lines[0].matches("[1-9][0-9]{0,9}")) {
            throw new IOException("应用更新清单水位已损坏，无法安全判断是否为最新版本。");
        }
        try {
            UpdateManifestWatermark watermark = new UpdateManifestWatermark(
                    Long.parseLong(lines[0]),
                    lines[1],
                    lines[2]
            );
            watermark.validate();
            return watermark;
        } catch (NumberFormatException exception) {
            throw new IOException("应用更新清单水位已损坏，无法安全判断是否为最新版本。", exception);
        }
    }

    static void writeUpdateManifestWatermark(File watermarkFile, UpdateManifestWatermark watermark)
            throws IOException {
        watermark.validate();
        String payload = watermark.versionCode
                + "\n" + watermark.versionName
                + "\n" + watermark.manifestSha256 + "\n";
        writeAtomically(watermarkFile, payload.getBytes(StandardCharsets.US_ASCII));
    }

    private MarketData parseMarketDataSafely(byte[] manifestBytes, byte[] catalogueBytes, byte[] signalsBytes)
            throws IOException, JSONException {
        try {
            return parseMarketData(manifestBytes, catalogueBytes, signalsBytes);
        } catch (OutOfMemoryError memoryFailure) {
            throw new IOException("市场数据超过本机可安全处理的大小，已保留上一份数据。", memoryFailure);
        }
    }

    private MarketData parseMarketData(byte[] manifestBytes, byte[] catalogueBytes, byte[] signalsBytes)
            throws IOException, JSONException {
        JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
        AssetMeta catalogueMeta = catalogueMeta(manifest);
        AssetMeta signalsMeta = signalsMeta(manifest);
        validateGenerationFilenames(
                signatureFilename(manifest),
                catalogueMeta.filename,
                signalsMeta.filename
        );
        verifyAsset(catalogueBytes, catalogueMeta);
        verifyAsset(signalsBytes, signalsMeta);
        JSONObject catalogue = gunzipJson(catalogueBytes);
        JSONObject signals = gunzipJson(signalsBytes);
        validateSharedSnapshotFields(manifest, catalogue);
        validateSharedSnapshotFields(manifest, signals);
        SnapshotTime snapshotTime = validateSnapshotTime(
                manifest.optString("market_as_of"),
                manifest.optString("data_timestamp_utc")
        );
        Map<String, String> signalDetails = parseSignalDetails(signals);
        JSONArray companies = catalogue.optJSONArray("companies");
        if (companies == null
                || companies.length() != catalogue.optInt("company_count", -1)
                || !isSafeCompanyCount(companies.length())) {
            throw new IOException("市场目录为空或公司数量不一致。");
        }
        int companyCount = companies.length();
        validateAnalysisQuality(manifest, companyCount);
        validateAnalysisQuality(catalogue, companyCount);
        validateAnalysisQuality(signals, companyCount);
        Map<String, String> typeNames = parseTypeNames(catalogue.optJSONObject("type_names"));
        if (typeNames.size() != 7) {
            throw new IOException("市场数据缺少七种买入情况的中文名称。");
        }
        List<MarketEntry> entries = new ArrayList<>(companies.length());
        Set<String> uniqueCodes = new HashSet<>();
        for (int index = 0; index < companies.length(); index++) {
            JSONObject company = companies.optJSONObject(index);
            if (company == null) {
                throw new IOException("市场目录包含无效公司记录。");
            }
            MarketEntry entry = parseEntry(company, typeNames, signalDetails.get(company.optString("code")));
            if (!isShanghaiShenzhenCompanyCode(entry.code) || !uniqueCodes.add(entry.code)) {
                throw new IOException("市场目录包含非沪深公司或重复证券代码。");
            }
            entries.add(entry);
        }
        Collections.sort(entries);
        JSONObject summary = manifest.optJSONObject("summary");
        if (summary == null || summary.optInt("company_count", -1) != entries.size()) {
            throw new IOException("市场清单的公司总数与目录不一致。");
        }
        int triggered = 0;
        int conditional = 0;
        for (MarketEntry entry : entries) {
            if (entry.hasTriggeredSignal()) {
                triggered++;
            }
            if (entry.hasConditionalCandidate()) {
                conditional++;
            }
        }
        Set<String> candidateCodes = new HashSet<>();
        for (MarketEntry entry : entries) {
            if (entry.hasTriggeredSignal() || entry.hasConditionalCandidate()) {
                candidateCodes.add(entry.code);
            }
        }
        if (!candidateCodes.equals(signalDetails.keySet())
                || signals.optInt("candidate_detail_count", -1) != signalDetails.size()
                || signals.optInt("triggered_company_count", -1) != triggered
                || signals.optInt("conditional_company_count", -1) != conditional) {
            throw new IOException("候选详情与市场目录不属于同一批完整数据。");
        }
        if (summary.optInt("triggered_company_count", -1) != triggered
                || summary.optInt("conditional_company_count", -1) != conditional
                || summary.optInt("candidate_detail_count", -1) != signalDetails.size()) {
            throw new IOException("市场清单的买入信号数量与公司目录不一致。");
        }
        Map<String, Map<String, Integer>> typeCoverage = validateTypeCoverage(
                entries,
                summary.optJSONObject("type_coverage"),
                catalogue.optJSONObject("coverage")
        );
        return new MarketData(
                manifest.optString("market_as_of"),
                manifest.optString("data_timestamp_utc"),
                snapshotTime.marketDate,
                snapshotTime.dataTimestamp,
                companyCount,
                triggered,
                conditional,
                typeNames,
                typeCoverage,
                entries
        );
    }

    private static Map<String, Map<String, Integer>> validateTypeCoverage(
            List<MarketEntry> entries,
            JSONObject manifestCoverage,
            JSONObject catalogueCoverage
    ) throws IOException {
        if (manifestCoverage == null || catalogueCoverage == null
                || manifestCoverage.length() != 7 || catalogueCoverage.length() != 7) {
            throw new IOException("市场清单缺少完整的七类状态统计。");
        }
        Map<String, Map<String, Integer>> result = new HashMap<>();
        for (int number = 1; number <= 7; number++) {
            String typeKey = "type" + number;
            Map<String, Integer> actual = new HashMap<>();
            for (String status : VALID_TYPE_STATUSES) {
                actual.put(status, 0);
            }
            for (MarketEntry entry : entries) {
                TypeScore score = entry.typeScores.get(typeKey);
                if (score == null || !actual.containsKey(score.status)) {
                    throw new IOException("市场目录无法复算七类状态统计。");
                }
                actual.put(score.status, actual.get(score.status) + 1);
            }
            int statusTotal = 0;
            for (int count : actual.values()) {
                statusTotal += count;
            }
            if (statusTotal != entries.size()) {
                throw new IOException("市场目录的七类状态总数与公司总数不一致。");
            }
            JSONObject manifestType = manifestCoverage.optJSONObject(typeKey);
            JSONObject catalogueType = catalogueCoverage.optJSONObject(typeKey);
            if (manifestType == null || catalogueType == null
                    || manifestType.length() != VALID_TYPE_STATUSES.size()
                    || catalogueType.length() != VALID_TYPE_STATUSES.size()) {
                throw new IOException("市场清单的七类状态统计不完整。");
            }
            for (String status : VALID_TYPE_STATUSES) {
                int expected = actual.get(status);
                if (manifestType.optInt(status, -1) != expected
                        || catalogueType.optInt(status, -1) != expected) {
                    throw new IOException("市场清单的七类状态统计与公司目录不一致。");
                }
            }
            result.put(typeKey, Collections.unmodifiableMap(new HashMap<>(actual)));
        }
        return Collections.unmodifiableMap(result);
    }

    private static AssetMeta catalogueMeta(JSONObject manifest) throws IOException {
        if (manifest.optInt("schema_version", -1) != SNAPSHOT_SCHEMA_VERSION) {
            throw new IOException("市场数据版本不受支持，请升级应用。");
        }
        JSONObject asset = manifest.optJSONObject("catalogue");
        if (asset == null || !asset.optString("filename").matches("catalog-[0-9a-f]{16}\\.json\\.gz")) {
            throw new IOException("市场清单缺少目录文件说明。");
        }
        String sha256 = asset.optString("sha256").toLowerCase(Locale.ROOT);
        long size = asset.optLong("size", -1L);
        if (!sha256.matches("[0-9a-f]{64}") || size <= 0L || size > MAX_COMPRESSED_ASSET_BYTES) {
            throw new IOException("市场目录校验信息不合法。");
        }
        return new AssetMeta(asset.optString("filename"), sha256, size);
    }

    private static AssetMeta signalsMeta(JSONObject manifest) throws IOException {
        JSONObject asset = manifest.optJSONObject("signals");
        if (asset == null || !asset.optString("filename").matches("signals-[0-9a-f]{16}\\.json\\.gz")) {
            throw new IOException("市场清单缺少候选详情文件说明。");
        }
        String sha256 = asset.optString("sha256").toLowerCase(Locale.ROOT);
        long size = asset.optLong("size", -1L);
        if (!sha256.matches("[0-9a-f]{64}") || size <= 0L || size > MAX_COMPRESSED_ASSET_BYTES) {
            throw new IOException("候选详情校验信息不合法。");
        }
        return new AssetMeta(asset.optString("filename"), sha256, size);
    }

    private static String signatureFilename(JSONObject manifest) throws IOException {
        JSONObject signature = manifest.optJSONObject("signature");
        if (signature == null
                || !"ECDSA_P256_SHA256".equals(signature.optString("algorithm"))
                || !signature.optString("filename").matches("manifest-[0-9a-f]{16}\\.sig")) {
            throw new IOException("市场清单缺少可信的数据签名说明。");
        }
        return signature.optString("filename");
    }

    static void validateGenerationFilenames(
            String signatureFilename,
            String catalogueFilename,
            String signalsFilename
    ) throws IOException {
        if (signatureFilename == null
                || catalogueFilename == null
                || signalsFilename == null
                || !signatureFilename.matches("manifest-[0-9a-f]{16}\\.sig")
                || !catalogueFilename.matches("catalog-[0-9a-f]{16}\\.json\\.gz")
                || !signalsFilename.matches("signals-[0-9a-f]{16}\\.json\\.gz")) {
            throw new IOException("市场清单包含无效的数据文件名。");
        }
        String signatureGeneration = signatureFilename.substring("manifest-".length(), "manifest-".length() + 16);
        String catalogueGeneration = catalogueFilename.substring("catalog-".length(), "catalog-".length() + 16);
        String signalsGeneration = signalsFilename.substring("signals-".length(), "signals-".length() + 16);
        if (!signatureGeneration.equals(catalogueGeneration)
                || !signatureGeneration.equals(signalsGeneration)) {
            throw new IOException("市场清单引用了不同批次的数据文件。");
        }
    }

    private static void validateSharedSnapshotFields(JSONObject manifest, JSONObject catalogue) throws IOException {
        if (catalogue.optInt("schema_version", -1) != SNAPSHOT_SCHEMA_VERSION
                || !"DS_DCF".equals(manifest.optString("product"))
                || !"DS_DCF".equals(catalogue.optString("product"))
                || !manifest.optString("market_as_of").equals(catalogue.optString("market_as_of"))
                || !manifest.optString("data_timestamp_utc").equals(catalogue.optString("data_timestamp_utc"))) {
            throw new IOException("市场目录与清单不属于同一批数据。");
        }
    }

    private static SnapshotTime validateSnapshotTime(String marketAsOf, String dataTimestampUtc) throws IOException {
        try {
            LocalDate marketDate = LocalDate.parse(marketAsOf);
            Instant timestamp = Instant.parse(dataTimestampUtc);
            Instant now = Instant.now();
            if (timestamp.isAfter(now.plus(MAX_FUTURE_CLOCK_SKEW))) {
                throw new IOException("市场数据时间晚于当前时间，已拒绝使用。");
            }
            if (timestamp.isBefore(now.minus(MAX_SNAPSHOT_AGE))) {
                throw new IOException("市场数据已超过 14 天未更新，请稍后重新获取。");
            }
            LocalDate timestampInShanghai = timestamp.atZone(ZoneId.of("Asia/Shanghai")).toLocalDate();
            if (!marketDate.equals(timestampInShanghai)
                    || marketDate.isAfter(LocalDate.now(ZoneId.of("Asia/Shanghai")))) {
                throw new IOException("市场交易日与数据生成时间不一致。");
            }
            if (timestamp.atZone(ZoneId.of("Asia/Shanghai")).toLocalTime()
                    .isBefore(java.time.LocalTime.of(16, 0))) {
                throw new IOException("市场数据不是收盘后生成的，已拒绝使用。");
            }
            return new SnapshotTime(marketDate, timestamp);
        } catch (java.time.format.DateTimeParseException exception) {
            throw new IOException("市场交易日或数据时间格式不合法。", exception);
        }
    }

    private static void verifyManifestSignature(byte[] manifestBytes, byte[] signatureBytes) throws IOException {
        verifyDetachedSignature(
                manifestBytes,
                signatureBytes,
                MOBILE_SIGNING_PUBLIC_KEY_BASE64,
                "市场数据签名校验失败，已保留上一份数据。"
        );
    }

    static void verifyDetachedSignature(
            byte[] manifestBytes,
            byte[] signatureBytes,
            String publicKeyBase64,
            String invalidSignatureMessage
    ) throws IOException {
        if (manifestBytes == null
                || manifestBytes.length == 0
                || signatureBytes == null
                || signatureBytes.length == 0
                || signatureBytes.length > MAX_MANIFEST_SIGNATURE_BYTES
                || publicKeyBase64 == null
                || publicKeyBase64.isEmpty()) {
            throw new IOException(invalidSignatureMessage);
        }
        try {
            byte[] publicKeyBytes = Base64.getDecoder().decode(publicKeyBase64);
            PublicKey publicKey = KeyFactory.getInstance("EC").generatePublic(new X509EncodedKeySpec(publicKeyBytes));
            Signature verifier = Signature.getInstance("SHA256withECDSA");
            verifier.initVerify(publicKey);
            verifier.update(manifestBytes);
            if (!verifier.verify(signatureBytes)) {
                throw new IOException(invalidSignatureMessage);
            }
        } catch (IOException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IOException(invalidSignatureMessage, exception);
        }
    }

    static SnapshotWatermark selectSnapshotWatermark(
            SnapshotWatermark accepted,
            SnapshotWatermark candidate
    ) throws IOException {
        if (candidate == null) {
            return accepted;
        }
        candidate.validate();
        if (accepted == null) {
            return candidate;
        }
        accepted.validate();
        int marketDateOrder = candidate.marketDate.compareTo(accepted.marketDate);
        int timestampOrder = candidate.dataTimestamp.compareTo(accepted.dataTimestamp);
        if (marketDateOrder == 0 && timestampOrder == 0) {
            if (!candidate.manifestSha256.equals(accepted.manifestSha256)) {
                throw new SnapshotContentConflictException(
                        "同一市场交易日和生成时间出现了不同内容，已拒绝覆盖已验证数据。"
                );
            }
            return candidate;
        }
        if (marketDateOrder < 0 || (marketDateOrder == 0 && timestampOrder < 0)) {
            return accepted;
        }
        return candidate;
    }

    static SnapshotWatermark readSnapshotWatermark(File watermarkFile) throws IOException {
        if (!watermarkFile.isFile()) {
            return null;
        }
        String payload = new String(readFileLimited(watermarkFile, 256), StandardCharsets.US_ASCII);
        String[] lines = payload.split("\\n", -1);
        if (lines.length != 4 || !lines[3].isEmpty()) {
            throw new IOException("市场数据防回退水位已损坏，无法安全读取缓存。");
        }
        try {
            SnapshotWatermark watermark = new SnapshotWatermark(
                    LocalDate.parse(lines[0]),
                    Instant.parse(lines[1]),
                    lines[2]
            );
            watermark.validate();
            return watermark;
        } catch (java.time.format.DateTimeParseException exception) {
            throw new IOException("市场数据防回退水位已损坏，无法安全读取缓存。", exception);
        }
    }

    static void writeSnapshotWatermark(File watermarkFile, SnapshotWatermark watermark) throws IOException {
        watermark.validate();
        String payload = watermark.marketDate
                + "\n" + watermark.dataTimestamp
                + "\n" + watermark.manifestSha256 + "\n";
        writeAtomically(watermarkFile, payload.getBytes(StandardCharsets.US_ASCII));
    }

    private void persistGeneration(
            byte[] manifestBytes,
            byte[] signatureBytes,
            byte[] catalogueBytes,
            byte[] signalsBytes
    ) throws IOException {
        String generation = sha256(manifestBytes);
        File generationsDirectory = new File(cacheDirectory, "generations");
        if (!generationsDirectory.isDirectory() && !generationsDirectory.mkdirs()) {
            throw new IOException("无法创建手机历史数据目录。");
        }
        File finalDirectory = new File(generationsDirectory, generation);
        if (finalDirectory.isDirectory()) {
            try {
                loadGeneration(finalDirectory);
            } catch (IOException | JSONException corruptGeneration) {
                deleteRecursively(finalDirectory);
            }
        }
        if (!finalDirectory.isDirectory()) {
            File temporaryDirectory = new File(
                    generationsDirectory,
                    "." + generation + "." + Long.toUnsignedString(System.nanoTime()) + ".tmp"
            );
            if (!temporaryDirectory.mkdir()) {
                throw new IOException("无法创建手机临时缓存目录。");
            }
            boolean published = false;
            try {
                writeSynced(new File(temporaryDirectory, "manifest.json"), manifestBytes);
                writeSynced(new File(temporaryDirectory, "manifest.sig"), signatureBytes);
                writeSynced(new File(temporaryDirectory, "catalog.json.gz"), catalogueBytes);
                writeSynced(new File(temporaryDirectory, "signals.json.gz"), signalsBytes);
                try {
                    Files.move(temporaryDirectory.toPath(), finalDirectory.toPath(), StandardCopyOption.ATOMIC_MOVE);
                } catch (IOException atomicMoveFailure) {
                    Files.move(temporaryDirectory.toPath(), finalDirectory.toPath());
                }
                published = true;
            } finally {
                if (!published) {
                    deleteRecursively(temporaryDirectory);
                }
            }
        }
        writeAtomically(
                new File(cacheDirectory, "active-generation"),
                (generation + "\n").getBytes(StandardCharsets.US_ASCII)
        );
        manifestBytes = null;
        signatureBytes = null;
        catalogueBytes = null;
        signalsBytes = null;
        pruneInactiveGenerations(generationsDirectory, generation);
    }

    private static void writeSynced(File target, byte[] bytes) throws IOException {
        try (FileOutputStream output = new FileOutputStream(target)) {
            output.write(bytes);
            output.getFD().sync();
        }
    }

    private void pruneInactiveGenerations(File generationsDirectory, String activeGeneration) {
        File[] directories = generationsDirectory.listFiles(
                file -> file.isDirectory() && file.getName().matches("[0-9a-f]{64}")
        );
        if (directories == null) {
            return;
        }
        List<GenerationDirectory> validInactive = new ArrayList<>();
        for (File directory : directories) {
            if (directory.getName().equals(activeGeneration)) {
                continue;
            }
            try {
                MarketData verified = loadGeneration(directory);
                validInactive.add(new GenerationDirectory(
                        directory,
                        verified.marketDate,
                        verified.dataTimestamp
                ));
            } catch (IOException | JSONException invalidGeneration) {
                deleteRecursively(directory);
            }
        }
        validInactive.sort((left, right) -> {
            int marketDateOrder = right.marketDate.compareTo(left.marketDate);
            if (marketDateOrder != 0) {
                return marketDateOrder;
            }
            int timestampOrder = right.dataTimestamp.compareTo(left.dataTimestamp);
            if (timestampOrder != 0) {
                return timestampOrder;
            }
            return Long.compare(right.directory.lastModified(), left.directory.lastModified());
        });
        for (int index = 1; index < validInactive.size(); index++) {
            deleteRecursively(validInactive.get(index).directory);
        }
    }

    private static void deleteRecursively(File target) {
        File[] children = target.listFiles();
        if (children != null) {
            for (File child : children) {
                deleteRecursively(child);
            }
        }
        if (!target.delete()) {
            target.deleteOnExit();
        }
    }

    private static void validateAnalysisQuality(JSONObject snapshot, int companyCount) throws IOException {
        JSONObject quality = snapshot.optJSONObject("analysis_quality");
        double coverage = quality == null ? Double.NaN : quality.optDouble("score_coverage", Double.NaN);
        if (quality == null
                || !quality.optBoolean("ok", false)
                || quality.optInt("expected_companies", -1) != companyCount
                || quality.optInt("score_raw_rows", -1) != companyCount
                || quality.optInt("score_rows", -1) != companyCount
                || !Double.isFinite(coverage)
                || coverage < 0.99
                || coverage > 1.0
                || quality.optInt("pipeline_issues", -1) != 0) {
            throw new IOException("市场数据没有通过服务器完整性检查。");
        }
    }

    static boolean isSafeCompanyCount(int companyCount) {
        return companyCount >= MIN_SH_SZ_COMPANY_COUNT && companyCount <= MAX_SH_SZ_COMPANY_COUNT;
    }

    static boolean isShanghaiShenzhenCompanyCode(String code) {
        return code != null && code.matches("[036][0-9]{5}");
    }

    static boolean matchesSignalFilter(
            List<String> buyTypes,
            List<String> conditionalTypes,
            Map<String, TypeScore> typeScores,
            int displayMode,
            int typeNumber
    ) {
        if (buyTypes == null
                || conditionalTypes == null
                || typeScores == null
                || displayMode < 0
                || displayMode > 3
                || typeNumber < 0
                || typeNumber > 7) {
            return false;
        }
        if (typeNumber == 0) {
            if (displayMode == 0) {
                return !buyTypes.isEmpty();
            }
            if (displayMode == 1) {
                return !conditionalTypes.isEmpty();
            }
            if (displayMode == 2) {
                return hasTypeStatus(typeScores, "observe");
            }
            return true;
        }
        String selectedType = "type" + typeNumber;
        if (displayMode == 0) {
            return buyTypes.contains(selectedType);
        }
        if (displayMode == 1) {
            return conditionalTypes.contains(selectedType);
        }
        TypeScore score = typeScores.get(selectedType);
        if (displayMode == 2) {
            return score != null && "observe".equals(score.status);
        }
        return score != null && !"not_applicable".equals(score.status);
    }

    private static boolean hasTypeStatus(Map<String, TypeScore> typeScores, String status) {
        for (TypeScore score : typeScores.values()) {
            if (score != null && status.equals(score.status)) {
                return true;
            }
        }
        return false;
    }

    static boolean isValidTypeKeyList(List<String> values) {
        if (values == null) {
            return false;
        }
        Set<String> unique = new HashSet<>();
        for (String value : values) {
            if (value == null || !value.matches("type[1-7]") || !unique.add(value)) {
                return false;
            }
        }
        return true;
    }

    static boolean isRecognizedTypeStatus(String status) {
        return status != null && VALID_TYPE_STATUSES.contains(status);
    }

    static boolean isScorelessTypeStatus(String status) {
        return "not_applicable".equals(status) || "insufficient_evidence".equals(status);
    }

    static String publicReasonText(String value) {
        String text = value == null ? "" : value.trim();
        if (text.isEmpty()) {
            return "";
        }
        String lower = text.toLowerCase(Locale.ROOT);
        String machineProbe = lower.replace('\r', ' ').replace('\n', ' ');
        boolean machineText = machineProbe.matches(".*patch[0-9]+[-_].*")
                || machineProbe.contains("model_id")
                || machineProbe.contains("schema_version")
                || machineProbe.contains("formula")
                || machineProbe.contains("derived_proxy")
                || machineProbe.contains("validation_status")
                || machineProbe.contains("evidence_level")
                || machineProbe.contains("source_rule")
                || machineProbe.contains("opt_upper_v")
                || machineProbe.contains("normalised_roe")
                || machineProbe.contains("normalized_roe")
                || machineProbe.contains("cost_of_equity")
                || machineProbe.contains("financial_fade_horizon")
                || machineProbe.matches(".*\\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\\b.*")
                || machineProbe.matches(".*\\b[a-z][a-z0-9_]{2,}\\s*=.*");
        if (!machineText) {
            return text;
        }
        if (lower.contains("type2c") || text.contains("量价") || text.contains("换手")) {
            return "量价与换手数据";
        }
        return "可核验的财务与行业数据";
    }

    private static Map<String, String> parseTypeNames(JSONObject value) {
        Map<String, String> result = new HashMap<>();
        Set<String> uniqueLabels = new HashSet<>();
        if (value != null && value.length() == 7) {
            Iterator<String> keys = value.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                String label = value.optString(key).trim();
                if (key.matches("type[1-7]")
                        && !label.isEmpty()
                        && label.length() <= 40
                        && uniqueLabels.add(label)) {
                    result.put(key, label);
                }
            }
        }
        return result;
    }

    private static Map<String, String> parseSignalDetails(JSONObject signals) throws IOException {
        JSONArray values = signals.optJSONArray("signals");
        if (values == null) {
            throw new IOException("候选详情文件缺少公司记录。");
        }
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < values.length(); index++) {
            JSONObject value = values.optJSONObject(index);
            if (value == null) {
                throw new IOException("候选详情文件包含无效记录。");
            }
            String code = value.optString("code");
            String detailText = value.optString("detail_text").trim();
            if (!code.matches("[0-9]{6}") || detailText.isEmpty() || result.put(code, detailText) != null) {
                throw new IOException("候选详情包含无效或重复的证券代码。");
            }
        }
        return result;
    }

    private static MarketEntry parseEntry(
            JSONObject company,
            Map<String, String> typeNames,
            String detailText
    ) throws IOException {
        if (typeNames.size() != 7) {
            throw new IOException("市场目录缺少七种买入情况的中文名称。");
        }
        Map<String, TypeScore> typeScores = new HashMap<>();
        JSONObject types = company.optJSONObject("types");
        if (types == null || types.length() != 7) {
            throw new IOException("公司记录缺少七种买入情况的评分状态。");
        }
        for (int number = 1; number <= 7; number++) {
            String key = "type" + number;
            JSONObject type = types.optJSONObject(key);
            if (type == null) {
                throw new IOException("公司记录缺少 " + typeNames.get(key) + " 的评分状态。");
            }
            String status = type.optString("status", "invalid");
            if (!isRecognizedTypeStatus(status)) {
                throw new IOException("公司记录包含无法识别的买入情况状态。");
            }
            Double score = null;
            if (!type.isNull("score")) {
                double rawScore = type.optDouble("score", Double.NaN);
                if (!Double.isFinite(rawScore) || rawScore < 0.0 || rawScore > 10.0) {
                    throw new IOException("公司记录包含超出 0 至 10 分范围的评分。");
                }
                score = rawScore;
            }
            String reason = publicReasonText(type.optString("reason"));
            if (reason.length() > MAX_PUBLIC_REASON_LENGTH) {
                throw new IOException("公司记录包含过长的买入情况说明。");
            }
            typeScores.put(key, new TypeScore(status, score, reason));
        }
        List<String> buyTypes = toTypeKeyList(company.optJSONArray("buy_types"), "实际买入类型");
        List<String> conditionalTypes = toTypeKeyList(company.optJSONArray("conditional_types"), "待确认类型");
        for (int number = 1; number <= 7; number++) {
            String key = "type" + number;
            String status = typeScores.get(key).status;
            if (buyTypes.contains(key) != "triggered".equals(status)
                    || conditionalTypes.contains(key) != "conditional".equals(status)) {
                throw new IOException("公司记录的买入标记与七类评分状态不一致。");
            }
        }
        return new MarketEntry(
                company.optString("code"),
                company.optString("name", "未命名公司"),
                company.optString("industry", ""),
                nullableDouble(company, "price"),
                buyTypes,
                conditionalTypes,
                typeScores,
                typeNames,
                detailText
        );
    }

    private static Double nullableDouble(JSONObject object, String key) throws IOException {
        if (!object.has(key) || object.isNull(key)) {
            return null;
        }
        double value = object.optDouble(key, Double.NaN);
        if (!Double.isFinite(value) || value <= 0.0) {
            throw new IOException("公司记录包含无效价格。");
        }
        return value;
    }

    private static List<String> toTypeKeyList(JSONArray values, String label) throws IOException {
        if (values == null) {
            throw new IOException("公司记录缺少" + label + "。");
        }
        List<String> result = new ArrayList<>();
        for (int index = 0; index < values.length(); index++) {
            Object rawValue = values.opt(index);
            if (!(rawValue instanceof String)) {
                throw new IOException("公司记录的" + label + "格式不正确。");
            }
            result.add(((String) rawValue).trim());
        }
        if (!isValidTypeKeyList(result)) {
            throw new IOException("公司记录包含未知或重复的" + label + "。");
        }
        return result;
    }

    private static JSONObject gunzipJson(byte[] compressed) throws IOException, JSONException {
        try (InputStream gzip = new GZIPInputStream(new BufferedInputStream(new java.io.ByteArrayInputStream(compressed)))) {
            byte[] raw = readLimited(gzip, MAX_UNCOMPRESSED_ASSET_BYTES);
            return new JSONObject(new String(raw, StandardCharsets.UTF_8));
        }
    }

    private static void verifyAsset(byte[] bytes, AssetMeta meta) throws IOException {
        if (bytes.length != meta.size || !sha256(bytes).equals(meta.sha256)) {
            throw new IOException("市场数据文件完整性校验失败，已保留上一份数据。");
        }
    }

    private static byte[] download(String address, long maxBytes) throws IOException {
        URL current = new URL(address);
        for (int redirects = 0; redirects <= 5; redirects++) {
            if (!isTrustedDownloadUrl(current.toString())) {
                throw new IOException("下载地址不是受信任的官方加密发布地址。");
            }
            HttpURLConnection connection = (HttpURLConnection) current.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(15_000);
            connection.setReadTimeout(30_000);
            connection.setRequestProperty("Cache-Control", "no-cache, no-store, max-age=0");
            connection.setRequestProperty("Pragma", "no-cache");
            connection.setRequestProperty("Accept", "application/json, application/gzip, application/vnd.android.package-archive");
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) {
                String location = connection.getHeaderField("Location");
                connection.disconnect();
                if (location == null || location.trim().isEmpty()) {
                    throw new IOException("下载跳转没有目标地址。");
                }
                current = new URL(current, location);
                continue;
            }
            if (status != HttpURLConnection.HTTP_OK) {
                connection.disconnect();
                throw new IOException("下载服务器返回 HTTP " + status + "。");
            }
            long length = connection.getContentLengthLong();
            if (length > maxBytes) {
                connection.disconnect();
                throw new IOException("下载文件大小不在允许范围内。");
            }
            try (InputStream input = connection.getInputStream()) {
                return readLimited(input, maxBytes);
            } finally {
                connection.disconnect();
            }
        }
        throw new IOException("下载跳转次数过多。");
    }

    private static void downloadVerifiedFile(
            String address,
            long expectedBytes,
            String expectedSha256,
            File target
    ) throws IOException {
        if (expectedBytes <= 0L
                || expectedBytes > MAX_APK_BYTES
                || expectedSha256 == null
                || !expectedSha256.matches("[0-9a-f]{64}")) {
            throw new IOException("官方更新说明中的安装包校验信息不合法。");
        }
        URL current = new URL(address);
        boolean completed = false;
        try {
            for (int redirects = 0; redirects <= 5; redirects++) {
                if (!isTrustedDownloadUrl(current.toString())) {
                    throw new IOException("下载地址不是受信任的官方加密发布地址。");
                }
                HttpURLConnection connection = (HttpURLConnection) current.openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(15_000);
                connection.setReadTimeout(30_000);
                connection.setRequestProperty("Accept", "application/vnd.android.package-archive");
                int status = connection.getResponseCode();
                if (status >= 300 && status < 400) {
                    String location = connection.getHeaderField("Location");
                    connection.disconnect();
                    if (location == null || location.trim().isEmpty()) {
                        throw new IOException("下载跳转没有目标地址。");
                    }
                    current = new URL(current, location);
                    continue;
                }
                if (status != HttpURLConnection.HTTP_OK) {
                    connection.disconnect();
                    throw new IOException("下载服务器返回 HTTP " + status + "。");
                }
                long announcedLength = connection.getContentLengthLong();
                if (announcedLength > 0L && announcedLength != expectedBytes) {
                    connection.disconnect();
                    throw new IOException("安装包文件大小与官方更新说明不一致。");
                }
                MessageDigest digest;
                try {
                    digest = MessageDigest.getInstance("SHA-256");
                } catch (NoSuchAlgorithmException exception) {
                    connection.disconnect();
                    throw new IOException("系统不支持安装包完整性校验。", exception);
                }
                long received = 0L;
                try (InputStream input = connection.getInputStream();
                     FileOutputStream output = new FileOutputStream(target)) {
                    byte[] buffer = new byte[64 * 1024];
                    int count;
                    while ((count = input.read(buffer)) != -1) {
                        received += count;
                        if (received > expectedBytes || received > MAX_APK_BYTES) {
                            throw new IOException("安装包超过允许的大小，已停止下载。");
                        }
                        digest.update(buffer, 0, count);
                        output.write(buffer, 0, count);
                    }
                    output.getFD().sync();
                } finally {
                    connection.disconnect();
                }
                String actualSha256 = hex(digest.digest());
                if (received != expectedBytes || !actualSha256.equals(expectedSha256)) {
                    throw new IOException("安装包文件完整性校验失败，已拒绝安装。");
                }
                completed = true;
                return;
            }
            throw new IOException("下载跳转次数过多。");
        } finally {
            if (!completed && target.exists() && !target.delete()) {
                target.deleteOnExit();
            }
        }
    }

    static boolean isTrustedDownloadUrl(String address) {
        try {
            URL url = new URL(address);
            return "https".equalsIgnoreCase(url.getProtocol()) && TRUSTED_DOWNLOAD_HOSTS.contains(url.getHost().toLowerCase(Locale.ROOT));
        } catch (Exception exception) {
            return false;
        }
    }

    static boolean isExpectedReleaseSignerHash(String value) {
        return RELEASE_CERT_SHA256.equals(value);
    }

    private static byte[] readFileLimited(File file, int maxBytes) throws IOException {
        if (file.length() < 0L || file.length() > maxBytes) {
            throw new IOException("本地缓存文件大小不合法。");
        }
        try (InputStream input = new FileInputStream(file)) {
            return readLimited(input, maxBytes);
        }
    }

    private static byte[] readLimited(InputStream input, long maxBytes) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[16 * 1024];
        long total = 0L;
        int count;
        while ((count = input.read(buffer)) != -1) {
            total += count;
            if (total > maxBytes) {
                throw new IOException("下载内容超过安全上限。");
            }
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static String sha256(byte[] bytes) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return hex(digest.digest(bytes));
        } catch (NoSuchAlgorithmException exception) {
            throw new IOException("系统不支持文件完整性校验。", exception);
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder builder = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            builder.append(String.format(Locale.ROOT, "%02x", value));
        }
        return builder.toString();
    }

    private static void writeAtomically(File target, byte[] bytes) throws IOException {
        File parent = target.getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("无法创建手机数据缓存目录。");
        }
        File temporary = File.createTempFile(target.getName() + ".", ".tmp", parent);
        try (FileOutputStream output = new FileOutputStream(temporary)) {
            output.write(bytes);
            output.getFD().sync();
        }
        try {
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException atomicMoveFailure) {
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private static final class AssetMeta {
        final String filename;
        final String sha256;
        final long size;

        AssetMeta(String filename, String sha256, long size) {
            this.filename = filename;
            this.sha256 = sha256;
            this.size = size;
        }
    }

    private static final class SnapshotTime {
        final LocalDate marketDate;
        final Instant dataTimestamp;

        SnapshotTime(LocalDate marketDate, Instant dataTimestamp) {
            this.marketDate = marketDate;
            this.dataTimestamp = dataTimestamp;
        }
    }

    static final class SnapshotWatermark {
        final LocalDate marketDate;
        final Instant dataTimestamp;
        final String manifestSha256;

        SnapshotWatermark(LocalDate marketDate, Instant dataTimestamp, String manifestSha256) {
            this.marketDate = marketDate;
            this.dataTimestamp = dataTimestamp;
            this.manifestSha256 = manifestSha256;
        }

        void validate() throws IOException {
            if (marketDate == null
                    || dataTimestamp == null
                    || manifestSha256 == null
                    || !manifestSha256.matches("[0-9a-f]{64}")
                    || !marketDate.equals(dataTimestamp.atZone(ZoneId.of("Asia/Shanghai")).toLocalDate())) {
                throw new IOException("市场数据防回退水位不合法。");
            }
        }
    }

    private static final class SnapshotContentConflictException extends IOException {
        SnapshotContentConflictException(String message) {
            super(message);
        }
    }

    private static final class GenerationDirectory {
        final File directory;
        final LocalDate marketDate;
        final Instant dataTimestamp;

        GenerationDirectory(File directory, LocalDate marketDate, Instant dataTimestamp) {
            this.directory = directory;
            this.marketDate = marketDate;
            this.dataTimestamp = dataTimestamp;
        }
    }

    public static final class UpdateInfo {
        public final long versionCode;
        public final String versionName;
        public final String url;
        public final String sha256;
        public final String signerSha256;
        public final long size;

        UpdateInfo(long versionCode, String versionName, String url, String sha256, String signerSha256, long size) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.url = url;
            this.sha256 = sha256;
            this.signerSha256 = signerSha256;
            this.size = size;
        }
    }

    static final class UpdateManifestWatermark {
        final long versionCode;
        final String versionName;
        final String manifestSha256;

        UpdateManifestWatermark(long versionCode, String versionName, String manifestSha256) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.manifestSha256 = manifestSha256;
        }

        void validate() throws IOException {
            if (versionCode <= 0L
                    || versionCode > Integer.MAX_VALUE
                    || versionName == null
                    || !versionName.matches("[0-9]+(?:\\.[0-9]+){2}(?:[-.][A-Za-z0-9]+)*")
                    || versionName.length() > 64
                    || manifestSha256 == null
                    || !manifestSha256.matches("[0-9a-f]{64}")) {
                throw new IOException("应用更新清单水位已损坏，无法安全判断是否为最新版本。");
            }
        }
    }

    public static final class MarketData {
        public final String marketAsOf;
        public final String dataTimestampUtc;
        public final LocalDate marketDate;
        public final Instant dataTimestamp;
        public final int companyCount;
        public final int triggeredCompanyCount;
        public final int conditionalCompanyCount;
        public final Map<String, String> typeNames;
        public final Map<String, Map<String, Integer>> typeCoverage;
        public final List<MarketEntry> entries;

        MarketData(String marketAsOf, String dataTimestampUtc, LocalDate marketDate, Instant dataTimestamp,
                   int companyCount, int triggeredCompanyCount,
                   int conditionalCompanyCount, Map<String, String> typeNames,
                   Map<String, Map<String, Integer>> typeCoverage, List<MarketEntry> entries) {
            this.marketAsOf = marketAsOf;
            this.dataTimestampUtc = dataTimestampUtc;
            this.marketDate = marketDate;
            this.dataTimestamp = dataTimestamp;
            this.companyCount = companyCount;
            this.triggeredCompanyCount = triggeredCompanyCount;
            this.conditionalCompanyCount = conditionalCompanyCount;
            this.typeNames = Collections.unmodifiableMap(new HashMap<>(typeNames));
            this.typeCoverage = Collections.unmodifiableMap(new HashMap<>(typeCoverage));
            this.entries = Collections.unmodifiableList(new ArrayList<>(entries));
        }

        public String typeCoverageSummary() {
            List<String> parts = new ArrayList<>();
            for (int number = 1; number <= 7; number++) {
                String key = "type" + number;
                Map<String, Integer> coverage = typeCoverage.get(key);
                int triggered = coverage == null ? 0 : coverage.getOrDefault("triggered", 0);
                int conditional = coverage == null ? 0 : coverage.getOrDefault("conditional", 0);
                int observe = coverage == null ? 0 : coverage.getOrDefault("observe", 0);
                String label = typeNames.getOrDefault(key, number + "类");
                parts.add(label + "：实际" + triggered + "家，待确认" + conditional
                        + "家，观察" + observe + "家");
            }
            return "七类结果：\n" + String.join("\n", parts);
        }
    }

    public static final class MarketEntry implements Comparable<MarketEntry> {
        public final String code;
        public final String name;
        public final String industry;
        public final Double price;
        public final List<String> buyTypes;
        public final List<String> conditionalTypes;
        public final Map<String, TypeScore> typeScores;
        public final String searchText;
        private final Map<String, String> typeNames;
        private final String detailText;

        MarketEntry(String code, String name, String industry, Double price, List<String> buyTypes,
                    List<String> conditionalTypes, Map<String, TypeScore> typeScores,
                    Map<String, String> typeNames, String detailText) {
            this.code = code;
            this.name = name;
            this.industry = publicIndustryLabel(industry);
            this.price = price;
            this.buyTypes = Collections.unmodifiableList(new ArrayList<>(buyTypes));
            this.conditionalTypes = Collections.unmodifiableList(new ArrayList<>(conditionalTypes));
            this.typeScores = Collections.unmodifiableMap(new HashMap<>(typeScores));
            this.searchText = (code + " " + name + " " + this.industry).toLowerCase(Locale.ROOT);
            this.typeNames = typeNames;
            this.detailText = publicReasonText(detailText);
        }

        private static String publicIndustryLabel(String value) {
            String text = value == null ? "" : value.trim();
            if (text.isEmpty()) {
                return "行业未知";
            }
            // New catalogues provide the Chinese public label.  This guard is
            // also needed for an older on-device cache so internal enums such
            // as ALCOHOL can never leak into displayLabel or search results.
            if (text.matches("[A-Z][A-Z0-9_]*")) {
                return "未分类行业";
            }
            return text;
        }

        public boolean hasTriggeredSignal() {
            return !buyTypes.isEmpty();
        }

        public boolean hasConditionalCandidate() {
            return !conditionalTypes.isEmpty();
        }

        public boolean hasObservedFramework() {
            return hasTypeStatus(typeScores, "observe");
        }

        public String displayLabel() {
            String priceText = price == null ? "价格暂无" : String.format(Locale.CHINA, "%.2f", price);
            List<String> labels = new ArrayList<>();
            if (hasTriggeredSignal()) {
                labels.add("买入信号：" + labels(buyTypes));
            }
            if (hasConditionalCandidate()) {
                labels.add("待确认：" + labels(conditionalTypes) + "（不是买入信号）");
            }
            List<String> observedTypes = typesWithStatus("observe");
            if (!observedTypes.isEmpty()) {
                labels.add("观察：" + labels(observedTypes) + "（不是买入信号）");
            }
            if (labels.isEmpty()) {
                labels.add("未触发买入信号");
            }
            return name + "  " + code + "\n" + priceText + "  " + industry + "\n" + String.join("\n", labels);
        }

        public String detailedLabel() {
            StringBuilder output = new StringBuilder(displayLabel());
            output.append("\n\n七类评分状态：");
            for (int number = 1; number <= 7; number++) {
                String key = "type" + number;
                TypeScore score = typeScores.get(key);
                output.append("\n").append(typeNames.containsKey(key) ? typeNames.get(key) : key)
                        .append("：").append(score == null ? "暂无数据" : score.describe());
            }
            if (detailText != null && !detailText.trim().isEmpty()) {
                output.append("\n\n服务器核验详情：\n").append(detailText);
            }
            return output.toString();
        }

        private String labels(List<String> types) {
            List<String> labels = new ArrayList<>();
            for (String type : types) {
                labels.add(typeNames.containsKey(type) ? typeNames.get(type) : type);
            }
            return String.join("、", labels);
        }

        private List<String> typesWithStatus(String status) {
            List<String> types = new ArrayList<>();
            for (int number = 1; number <= 7; number++) {
                String key = "type" + number;
                TypeScore score = typeScores.get(key);
                if (score != null && status.equals(score.status)) {
                    types.add(key);
                }
            }
            return types;
        }

        @Override
        public int compareTo(MarketEntry other) {
            int priority = (hasTriggeredSignal() ? 0 : hasConditionalCandidate() ? 1 : hasObservedFramework() ? 2 : 3);
            int otherPriority = (other.hasTriggeredSignal() ? 0
                    : other.hasConditionalCandidate() ? 1 : other.hasObservedFramework() ? 2 : 3);
            if (priority != otherPriority) {
                return Integer.compare(priority, otherPriority);
            }
            return code.compareTo(other.code);
        }
    }

    public static final class TypeScore {
        public final String status;
        public final Double score;
        public final String reason;

        TypeScore(String status, Double score, String reason) {
            this.status = status;
            // Older signed server generations may still contain the scorer's
            // internal 0.0/0.9 placeholders.  Applicability and evidence state
            // are authoritative: neither state has a user-facing numeric score.
            this.score = isScorelessTypeStatus(status) ? null : score;
            this.reason = publicReasonText(reason);
        }

        String describe() {
            String text;
            switch (status) {
                case "triggered": text = "已触发"; break;
                case "conditional": text = "待确认（不是买入信号）"; break;
                case "observe": text = "观察"; break;
                case "insufficient_evidence": text = "资料不足"; break;
                case "vetoed": text = "不符合硬条件"; break;
                case "blocked": text = "存在阻断条件"; break;
                case "not_applicable": text = "不适用"; break;
                case "not_triggered": text = "未触发"; break;
                default: text = "资料异常"; break;
            }
            String scored = score == null || isScorelessTypeStatus(status)
                    ? text
                    : String.format(Locale.CHINA, "%s，%.1f分", text, score);
            return reason == null || reason.isEmpty() ? scored : scored + "；" + reason;
        }
    }
}
