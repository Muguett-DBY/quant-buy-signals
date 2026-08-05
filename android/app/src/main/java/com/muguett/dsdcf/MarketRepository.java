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
    public static final String MOBILE_PAGES_ASSET_BASE =
            "https://muguett-dby.github.io/quant-buy-signals/mobile-data/";
    public static final String UPDATE_MANIFEST_URL =
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.json";
    public static final String UPDATE_MANIFEST_SIGNATURE_URL =
            "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/android-app/android-update-manifest.sig";

    private static final int SNAPSHOT_SCHEMA_VERSION = 1;
    private static final int MAX_MANIFEST_BYTES = 1_000_000;
    private static final int MAX_MANIFEST_SIGNATURE_BYTES = 1_024;
    private static final int MAX_COMPRESSED_ASSET_BYTES = 8_000_000;
    private static final int MAX_UNCOMPRESSED_ASSET_BYTES = 32_000_000;
    private static final int MAX_UPDATE_MANIFEST_BYTES = 1_000_000;
    private static final int MAX_PUBLIC_REASON_LENGTH = 200;
    private static final int DECISION_SCHEMA_VERSION = 1;
    private static final String DECISION_MODEL_ID = "buy-decision-bounds-v1";
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
    private static final Set<String> VALID_DECISION_BASES = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "full_evidence",
            "scope_exclusion",
            "confirmed_veto",
            "conservative_upper_bound",
            "action_condition",
            "market_block",
            "unresolved_missing_evidence"
    )));
    private static final Set<String> VALID_DECISION_VETO_STATES =
            Collections.unmodifiableSet(new HashSet<>(Arrays.asList("none", "possible", "confirmed")));
    private static final Map<String, List<String>> TYPE_DIMENSIONS = createTypeDimensions();
    private static final Map<String, String> DIMENSION_NAMES = createDimensionNames();
    private static final Set<String> DECISION_FIELDS = Collections.unmodifiableSet(new HashSet<>(Arrays.asList(
            "schema_version",
            "model_id",
            "decision_complete",
            "decision_basis",
            "score_lower_bound",
            "score_upper_bound",
            "veto_state",
            "potentially_triggerable",
            "missing_dimensions"
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
        return refresh(null);
    }

    public MarketData refresh(RefreshProgress progress) throws IOException, JSONException {
        reportProgress(progress, "正在连接数据服务…");
        byte[] manifestBytes = downloadFirstAvailable(
                new String[] {
                        MOBILE_MANIFEST_URL,
                        MOBILE_RELEASE_ASSET_BASE + "manifest.json"
                },
                MAX_MANIFEST_BYTES
        );
        JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
        String signatureFilename = signatureFilename(manifest);
        reportProgress(progress, "正在核对官方数据签名…");
        byte[] signatureBytes = downloadFirstAvailable(
                mobileAssetUrls(signatureFilename),
                MAX_MANIFEST_SIGNATURE_BYTES
        );
        verifyManifestSignature(manifestBytes, signatureBytes);
        AssetMeta catalogueMeta = catalogueMeta(manifest);
        AssetMeta signalsMeta = signalsMeta(manifest);
        validateGenerationFilenames(signatureFilename, catalogueMeta.filename, signalsMeta.filename);
        reportProgress(progress, "正在下载沪深公司目录…");
        byte[] catalogueBytes = downloadFirstAvailable(
                mobileAssetUrls(catalogueMeta.filename),
                MAX_COMPRESSED_ASSET_BYTES
        );
        reportProgress(progress, "正在下载七类买入情况结果…");
        byte[] signalsBytes = downloadFirstAvailable(
                mobileAssetUrls(signalsMeta.filename),
                MAX_COMPRESSED_ASSET_BYTES
        );
        reportProgress(progress, "正在校验并保存最新数据…");
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

    private static String[] mobileAssetUrls(String filename) {
        return new String[] {
                MOBILE_RELEASE_ASSET_BASE + filename,
                MOBILE_PAGES_ASSET_BASE + filename
        };
    }

    private static void reportProgress(RefreshProgress progress, String status) {
        if (progress != null) {
            progress.onStage(status);
        }
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
        boolean requireDimensionScores = publishesDimensionScores(manifest, catalogue);
        for (int index = 0; index < companies.length(); index++) {
            JSONObject company = companies.optJSONObject(index);
            if (company == null) {
                throw new IOException("市场目录包含无效公司记录。");
            }
            MarketEntry entry = parseEntry(
                    company,
                    typeNames,
                    signalDetails.get(company.optString("code")),
                    requireDimensionScores
            );
            if (!isShanghaiShenzhenCompanyCode(entry.code) || !uniqueCodes.add(entry.code)) {
                throw new IOException("市场目录包含非沪深公司或重复证券代码。");
            }
            entries.add(entry);
        }
        Collections.sort(entries);
        boolean decisionContractPresent = !entries.isEmpty() && entries.get(0).hasDecisionContract();
        for (MarketEntry entry : entries) {
            if (entry.hasDecisionContract() != decisionContractPresent) {
                throw new IOException("市场目录混合了不同版本的候选边界合同。");
            }
        }
        JSONObject summary = manifest.optJSONObject("summary");
        if (summary == null || summary.optInt("company_count", -1) != entries.size()) {
            throw new IOException("市场清单的公司总数与目录不一致。");
        }
        int triggered = 0;
        int conditional = 0;
        int conditionalOnly = 0;
        int pending = 0;
        for (MarketEntry entry : entries) {
            if (entry.hasTriggeredSignal()) {
                triggered++;
            }
            if (entry.hasConditionalCandidate()) {
                conditional++;
            }
            if (entry.hasConditionalOnlyCandidate()) {
                conditionalOnly++;
            }
            if (entry.hasPendingEvidenceCandidate()) {
                pending++;
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
                || signals.optInt("conditional_company_count", -1) != conditional
                || signals.optInt("conditional_only_company_count", -1) != conditionalOnly
                || (
                        signals.has("pending_company_count")
                                && signals.optInt("pending_company_count", -1) != pending
                )) {
            throw new IOException("候选详情与市场目录不属于同一批完整数据。");
        }
        if (summary.optInt("triggered_company_count", -1) != triggered
                || summary.optInt("conditional_company_count", -1) != conditional
                || summary.optInt("conditional_only_company_count", -1) != conditionalOnly
                || summary.optInt("candidate_detail_count", -1) != signalDetails.size()
                || (
                        summary.has("pending_company_count")
                                && summary.optInt("pending_company_count", -1) != pending
                )) {
            throw new IOException("市场清单的买入信号数量与公司目录不一致。");
        }
        int visibleCandidates = 0;
        for (MarketEntry entry : entries) {
            if (entry.hasTriggeredSignal()
                    || entry.hasConditionalCandidate()
                    || entry.hasPendingEvidenceCandidate()) {
                visibleCandidates++;
            }
        }
        if ((summary.has("visible_candidate_company_count")
                && summary.optInt("visible_candidate_company_count", -1) != visibleCandidates)
                || (signals.has("visible_candidate_company_count")
                && signals.optInt("visible_candidate_company_count", -1) != visibleCandidates)) {
            throw new IOException("市场清单的可见候选数量不一致。");
        }
        if (decisionContractPresent
                && (
                        !summary.has("pending_company_count")
                                || !summary.has("visible_candidate_company_count")
                                || !signals.has("pending_company_count")
                                || !signals.has("visible_candidate_company_count")
                )) {
            throw new IOException("市场清单缺少候选可见性计数。");
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
                pending,
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
            List<String> pendingTypes,
            Map<String, TypeScore> typeScores,
            int displayMode,
            int typeNumber
    ) {
        if (buyTypes == null
                || conditionalTypes == null
                || pendingTypes == null
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
                return !conditionalTypes.isEmpty() || !pendingTypes.isEmpty();
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
            return conditionalTypes.contains(selectedType) || pendingTypes.contains(selectedType);
        }
        TypeScore score = typeScores.get(selectedType);
        if (displayMode == 2) {
            return score != null && "observe".equals(score.status);
        }
        return score != null && !"not_applicable".equals(score.status);
    }

    static boolean matchesSignalFilter(
            List<String> buyTypes,
            List<String> conditionalTypes,
            Map<String, TypeScore> typeScores,
            int displayMode,
            int typeNumber
    ) {
        return matchesSignalFilter(
                buyTypes,
                conditionalTypes,
                Collections.emptyList(),
                typeScores,
                displayMode,
                typeNumber
        );
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

    private static boolean publishesDimensionScores(JSONObject manifest, JSONObject catalogue)
            throws IOException {
        JSONObject manifestCapabilities = manifest.optJSONObject("capabilities");
        JSONObject catalogueCapabilities = catalogue.optJSONObject("capabilities");
        boolean manifestPublished = manifestCapabilities != null
                && manifestCapabilities.optBoolean("dimension_scores", false);
        boolean cataloguePublished = catalogueCapabilities != null
                && catalogueCapabilities.optBoolean("dimension_scores", false);
        if (manifestPublished != cataloguePublished) {
            throw new IOException("市场清单与公司目录的子指标能力声明不一致。");
        }
        return manifestPublished;
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
            String detailText,
            boolean requireDimensionScores
    ) throws IOException {
        if (typeNames.size() != 7) {
            throw new IOException("市场目录缺少七种买入情况的中文名称。");
        }
        Map<String, TypeScore> typeScores = new HashMap<>();
        JSONObject types = company.optJSONObject("types");
        if (types == null || types.length() != 7) {
            throw new IOException("公司记录缺少七种买入情况的评分状态。");
        }
        int decisionContractCount = 0;
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
            DecisionSummary decision = null;
            if (type.has("decision")) {
                Object rawDecision = type.opt("decision");
                if (!(rawDecision instanceof JSONObject)) {
                    throw new IOException("公司记录包含格式错误的候选边界合同。");
                }
                decision = parseDecisionSummary((JSONObject) rawDecision, key);
            }
            if (decision != null) {
                decisionContractCount++;
            }
            Set<String> investorActionDimensions = parseInvestorActionDimensions(
                    type,
                    key,
                    decision
            );
            Map<String, Double> subScores = parseSubScores(
                    type.optJSONObject("sub_scores"),
                    key
            );
            Map<String, String> subScoreReasons = parseSubScoreReasons(
                    type.optJSONObject("sub_score_reasons"),
                    key,
                    subScores.keySet()
            );
            if (requireDimensionScores) {
                Set<String> expected = new HashSet<>();
                if (!"not_applicable".equals(status)) {
                    expected.addAll(TYPE_DIMENSIONS.get(key));
                    if ("insufficient_evidence".equals(status) && decision != null) {
                        expected.removeAll(decision.missingDimensions);
                    }
                    expected.removeAll(investorActionDimensions);
                }
                if (!subScores.keySet().equals(expected) || !subScoreReasons.keySet().equals(expected)) {
                    throw new IOException("公司记录缺少 " + typeNames.get(key) + " 的已知子指标分数或说明。");
                }
            }
            typeScores.put(key, new TypeScore(
                    status,
                    score,
                    reason,
                    decision,
                    subScores,
                    subScoreReasons,
                    investorActionDimensions
            ));
        }
        if (decisionContractCount != 0 && decisionContractCount != 7) {
            throw new IOException("公司记录的候选边界合同不完整。");
        }
        List<String> buyTypes = toTypeKeyList(company.optJSONArray("buy_types"), "实际买入类型");
        List<String> conditionalTypes = toTypeKeyList(company.optJSONArray("conditional_types"), "待确认类型");
        List<String> pendingTypes;
        if (decisionContractCount == 7) {
            pendingTypes = toTypeKeyList(company.optJSONArray("pending_types"), "待补证据类型");
        } else {
            // Backward compatibility for a still-valid signed 11.2 cache.
            // New server generations always publish all seven decisions and
            // the exact pending partition.
            pendingTypes = Collections.emptyList();
            if (company.has("pending_types")) {
                List<String> legacyPending =
                        toTypeKeyList(company.optJSONArray("pending_types"), "待补证据类型");
                if (!legacyPending.isEmpty()) {
                    throw new IOException("旧版市场目录不能声明待补证据类型。");
                }
            }
        }
        for (int number = 1; number <= 7; number++) {
            String key = "type" + number;
            TypeScore typeScore = typeScores.get(key);
            String status = typeScore.status;
            if (buyTypes.contains(key) != "triggered".equals(status)
                    || conditionalTypes.contains(key) != "conditional".equals(status)
                    || pendingTypes.contains(key) != (
                            typeScore.potentiallyTriggerable
                                    && !"triggered".equals(status)
                                    && !"conditional".equals(status)
                    )) {
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
                pendingTypes,
                typeScores,
                typeNames,
                detailText
        );
    }

    private static Set<String> parseInvestorActionDimensions(
            JSONObject type,
            String typeKey,
            DecisionSummary decision
    ) throws IOException {
        JSONArray dimensions = type.optJSONArray("investor_action_dimensions");
        // Older signed generations recorded the same semantic only through
        // ``action_required``.  Retain that read path so a valid cached
        // generation stays usable after the app updates.
        if (dimensions == null
                && "type6".equals(typeKey)
                && decision != null
                && decision.missingDimensions.contains("6e")
                && "position_confirmation".equals(type.optString("action_required"))) {
            return Collections.singleton("6e");
        }
        if (dimensions == null) {
            return Collections.emptySet();
        }
        if (!"type6".equals(typeKey) || decision == null) {
            throw new IOException("公司记录包含不适用的投资者动作维度。");
        }
        Set<String> result = new HashSet<>();
        for (int index = 0; index < dimensions.length(); index++) {
            Object raw = dimensions.opt(index);
            if (!(raw instanceof String)
                    || !"6e".equals(raw)
                    || !decision.missingDimensions.contains(raw)
                    || !result.add((String) raw)) {
                throw new IOException("公司记录包含无效的投资者动作维度。");
            }
        }
        if (result.size() != 1) {
            throw new IOException("公司记录缺少有效的投资者动作维度。");
        }
        return Collections.unmodifiableSet(result);
    }

    private static Map<String, Double> parseSubScores(JSONObject value, String typeKey)
            throws IOException {
        Map<String, Double> result = new HashMap<>();
        if (value == null) {
            return result;
        }
        List<String> allowed = TYPE_DIMENSIONS.get(typeKey);
        Iterator<String> keys = value.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            double score = value.optDouble(key, Double.NaN);
            if (allowed == null
                    || !allowed.contains(key)
                    || !Double.isFinite(score)
                    || score < 0.0
                    || score > 10.0
                    || result.put(key, score) != null) {
                throw new IOException("公司记录包含无效的子指标分数。");
            }
        }
        return result;
    }

    private static Map<String, String> parseSubScoreReasons(
            JSONObject value,
            String typeKey,
            Set<String> scoreKeys
    ) throws IOException {
        Map<String, String> result = new HashMap<>();
        if (value == null) {
            return result;
        }
        List<String> allowed = TYPE_DIMENSIONS.get(typeKey);
        Iterator<String> keys = value.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            String reason = publicReasonText(value.optString(key));
            if (allowed == null
                    || !allowed.contains(key)
                    || !scoreKeys.contains(key)
                    || reason.isEmpty()
                    || reason.length() > MAX_PUBLIC_REASON_LENGTH
                    || result.put(key, reason) != null) {
                throw new IOException("公司记录包含无效的子指标说明。");
            }
        }
        return result;
    }

    private static Map<String, List<String>> createTypeDimensions() {
        Map<String, List<String>> result = new HashMap<>();
        result.put("type1", Arrays.asList("1a", "1b", "1c", "1d"));
        result.put("type2", Arrays.asList("2a", "2b", "2c", "2d"));
        result.put("type3", Arrays.asList("3a", "3b", "3c", "3d", "3e"));
        result.put("type4", Arrays.asList("4a", "4b", "4c", "4d", "4e", "4f"));
        result.put("type5", Arrays.asList("5a", "5b", "5c", "5d", "5e"));
        result.put("type6", Arrays.asList("6a", "6b", "6c", "6d", "6e"));
        result.put("type7", Arrays.asList("7a", "7b", "7c"));
        return Collections.unmodifiableMap(result);
    }

    private static Map<String, String> createDimensionNames() {
        Map<String, String> result = new HashMap<>();
        String[][] values = {
                {"1a", "买入区深度"}, {"1b", "价值陷阱排查"}, {"1c", "安全边际厚度"}, {"1d", "催化剂/回归动力"},
                {"2a", "产业周期热度"}, {"2b", "公司周期拐点"}, {"2c", "市场周期冷度"}, {"2d", "估值合理性"},
                {"3a", "护城河支撑度"}, {"3b", "增长质量"}, {"3c", "资本回报率"}, {"3d", "增长可持续性"}, {"3e", "产业/股价泡沫"},
                {"4a", "坡的长度"}, {"4b", "雪的厚度"}, {"4c", "护城河耐久度"}, {"4d", "估值合理性"}, {"4e", "产业泡沫防范"}, {"4f", "股价泡沫防范"},
                {"5a", "强周期属性"}, {"5b", "底部信号"}, {"5c", "抗周期能力"}, {"5d", "上行弹性"}, {"5e", "正常化盈利估值"},
                {"6a", "产业爆发"}, {"6b", "技术壁垒"}, {"6c", "模式创新"}, {"6d", "困境反转"}, {"6e", "仓位风控"},
                {"7a", "本类别的商业模式"}, {"7b", "本类别的护城河"}, {"7c", "本类别的长期成长"}
        };
        for (String[] value : values) {
            result.put(value[0], value[1]);
        }
        return Collections.unmodifiableMap(result);
    }

    private static String publicScoreText(double value) {
        String formatted = String.format(Locale.CHINA, "%.3f", value);
        while (formatted.endsWith("0") && !formatted.endsWith(".0")) {
            formatted = formatted.substring(0, formatted.length() - 1);
        }
        return formatted;
    }

    private static DecisionSummary parseDecisionSummary(JSONObject decision, String typeKey) throws IOException {
        if (decision == null) {
            return null;
        }
        Set<String> fields = new HashSet<>();
        Iterator<String> keys = decision.keys();
        while (keys.hasNext()) {
            fields.add(keys.next());
        }
        Object rawSchemaVersion = decision.opt("schema_version");
        Object rawLower = decision.opt("score_lower_bound");
        Object rawUpper = decision.opt("score_upper_bound");
        if (!fields.equals(DECISION_FIELDS)
                || !(rawSchemaVersion instanceof Integer)
                || ((Integer) rawSchemaVersion) != DECISION_SCHEMA_VERSION
                || !DECISION_MODEL_ID.equals(decision.optString("model_id"))
                || !(decision.opt("decision_complete") instanceof Boolean)
                || !VALID_DECISION_BASES.contains(decision.optString("decision_basis"))
                || !VALID_DECISION_VETO_STATES.contains(decision.optString("veto_state"))
                || !(decision.opt("potentially_triggerable") instanceof Boolean)
                || !(rawLower instanceof Number)
                || !(rawUpper instanceof Number)) {
            throw new IOException("公司记录包含无效的候选边界合同。");
        }
        double lower = ((Number) rawLower).doubleValue();
        double upper = ((Number) rawUpper).doubleValue();
        if (!Double.isFinite(lower) || !Double.isFinite(upper) || lower < 0.0 || lower > upper || upper > 10.0) {
            throw new IOException("公司记录的候选分数边界无效。");
        }
        List<String> missingDimensions = new ArrayList<>();
        JSONArray missing = decision.optJSONArray("missing_dimensions");
        if (missing == null) {
            throw new IOException("公司记录的候选边界缺少待补维度。");
        }
        Set<String> unique = new HashSet<>();
        int typeNumber = Integer.parseInt(typeKey.substring(4));
        int dimensionCount = new int[]{0, 4, 4, 5, 6, 5, 5, 3}[typeNumber];
        String dimensionPattern = typeNumber + "[a-" + (char) ('a' + dimensionCount - 1) + "]";
        for (int index = 0; index < missing.length(); index++) {
            Object raw = missing.opt(index);
            if (!(raw instanceof String)) {
                throw new IOException("公司记录的待补维度格式不正确。");
            }
            String dimension = ((String) raw).trim();
            if (!dimension.matches(dimensionPattern) || !unique.add(dimension)) {
                throw new IOException("公司记录包含未知或重复的待补维度。");
            }
            missingDimensions.add(dimension);
        }
        return new DecisionSummary(
                decision.optBoolean("decision_complete"),
                decision.optString("decision_basis"),
                lower,
                upper,
                decision.optString("veto_state"),
                decision.optBoolean("potentially_triggerable"),
                missingDimensions
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

    private static byte[] downloadFirstAvailable(String[] addresses, long maxBytes) throws IOException {
        IOException lastFailure = null;
        for (String address : addresses) {
            try {
                return download(address, maxBytes);
            } catch (IOException failure) {
                lastFailure = failure;
            }
        }
        if (lastFailure == null) {
            throw new IOException("没有可用的官方下载地址。");
        }
        throw new IOException("所有官方下载地址均未能连接：" + safeDownloadFailure(lastFailure), lastFailure);
    }

    private static byte[] download(String address, long maxBytes) throws IOException {
        IOException lastFailure = null;
        for (int attempt = 1; attempt <= 2; attempt++) {
            try {
                return downloadOnce(address, maxBytes);
            } catch (IOException failure) {
                lastFailure = failure;
                if (attempt >= 2 || !isRetryableDownloadFailure(failure)) {
                    throw failure;
                }
                try {
                    Thread.sleep(300L * attempt);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new IOException("下载已被中止。", interrupted);
                }
            }
        }
        throw lastFailure == null ? new IOException("下载没有完成。") : lastFailure;
    }

    private static byte[] downloadOnce(String address, long maxBytes) throws IOException {
        URL current = new URL(address);
        for (int redirects = 0; redirects <= 5; redirects++) {
            if (!isTrustedDownloadUrl(current.toString())) {
                throw new IOException("下载地址不是受信任的官方加密发布地址。");
            }
            HttpURLConnection connection = (HttpURLConnection) current.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(25_000);
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

    static boolean isRetryableDownloadFailure(IOException failure) {
        String detail = failure.getMessage();
        if (detail == null || !detail.contains("HTTP ")) {
            return detail == null
                    || (!detail.contains("不在允许范围")
                    && !detail.contains("不是受信任")
                    && !detail.contains("跳转"));
        }
        return detail.contains("HTTP 408")
                || detail.contains("HTTP 425")
                || detail.contains("HTTP 429")
                || detail.contains("HTTP 500")
                || detail.contains("HTTP 502")
                || detail.contains("HTTP 503")
                || detail.contains("HTTP 504");
    }

    private static String safeDownloadFailure(IOException failure) {
        String detail = failure.getMessage();
        if (detail == null || detail.trim().isEmpty()) {
            return "请检查手机网络后重试。";
        }
        return detail;
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

    public interface RefreshProgress {
        void onStage(String status);
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
        public final int pendingCompanyCount;
        public final Map<String, String> typeNames;
        public final Map<String, Map<String, Integer>> typeCoverage;
        public final List<MarketEntry> entries;

        MarketData(String marketAsOf, String dataTimestampUtc, LocalDate marketDate, Instant dataTimestamp,
                   int companyCount, int triggeredCompanyCount,
                   int conditionalCompanyCount, int pendingCompanyCount, Map<String, String> typeNames,
                   Map<String, Map<String, Integer>> typeCoverage, List<MarketEntry> entries) {
            this.marketAsOf = marketAsOf;
            this.dataTimestampUtc = dataTimestampUtc;
            this.marketDate = marketDate;
            this.dataTimestamp = dataTimestamp;
            this.companyCount = companyCount;
            this.triggeredCompanyCount = triggeredCompanyCount;
            this.conditionalCompanyCount = conditionalCompanyCount;
            this.pendingCompanyCount = pendingCompanyCount;
            this.typeNames = Collections.unmodifiableMap(new HashMap<>(typeNames));
            this.typeCoverage = Collections.unmodifiableMap(new HashMap<>(typeCoverage));
            this.entries = Collections.unmodifiableList(new ArrayList<>(entries));
        }

        MarketData(String marketAsOf, String dataTimestampUtc, LocalDate marketDate, Instant dataTimestamp,
                   int companyCount, int triggeredCompanyCount,
                   int conditionalCompanyCount, Map<String, String> typeNames,
                   Map<String, Map<String, Integer>> typeCoverage, List<MarketEntry> entries) {
            this(
                    marketAsOf,
                    dataTimestampUtc,
                    marketDate,
                    dataTimestamp,
                    companyCount,
                    triggeredCompanyCount,
                    conditionalCompanyCount,
                    0,
                    typeNames,
                    typeCoverage,
                    entries
            );
        }

        public String typeCoverageSummary() {
            List<String> parts = new ArrayList<>();
            for (int number = 1; number <= 7; number++) {
                String key = "type" + number;
                Map<String, Integer> coverage = typeCoverage.get(key);
                int triggered = coverage == null ? 0 : coverage.getOrDefault("triggered", 0);
                int conditional = coverage == null ? 0 : coverage.getOrDefault("conditional", 0);
                int observe = coverage == null ? 0 : coverage.getOrDefault("observe", 0);
                int pending = 0;
                for (MarketEntry entry : entries) {
                    if (entry.pendingTypes.contains(key)) {
                        pending++;
                    }
                }
                String label = typeNames.getOrDefault(key, number + "类");
                parts.add(label + "：实际" + triggered + "家，待确认" + conditional
                        + "家，待补证据" + pending + "家，观察" + observe + "家");
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
        public final List<String> pendingTypes;
        public final Map<String, TypeScore> typeScores;
        public final String searchText;
        private final Map<String, String> typeNames;
        private final String detailText;

        MarketEntry(String code, String name, String industry, Double price, List<String> buyTypes,
                    List<String> conditionalTypes, List<String> pendingTypes, Map<String, TypeScore> typeScores,
                    Map<String, String> typeNames, String detailText) {
            this.code = code;
            this.name = name;
            this.industry = publicIndustryLabel(industry);
            this.price = price;
            this.buyTypes = Collections.unmodifiableList(new ArrayList<>(buyTypes));
            this.conditionalTypes = Collections.unmodifiableList(new ArrayList<>(conditionalTypes));
            this.pendingTypes = Collections.unmodifiableList(new ArrayList<>(pendingTypes));
            this.typeScores = Collections.unmodifiableMap(new HashMap<>(typeScores));
            this.searchText = (code + " " + name + " " + this.industry).toLowerCase(Locale.ROOT);
            this.typeNames = typeNames;
            this.detailText = publicReasonText(detailText);
        }

        MarketEntry(String code, String name, String industry, Double price, List<String> buyTypes,
                    List<String> conditionalTypes, Map<String, TypeScore> typeScores,
                    Map<String, String> typeNames, String detailText) {
            this(
                    code,
                    name,
                    industry,
                    price,
                    buyTypes,
                    conditionalTypes,
                    Collections.emptyList(),
                    typeScores,
                    typeNames,
                    detailText
            );
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

        public boolean hasConditionalOnlyCandidate() {
            return hasConditionalCandidate() && !hasTriggeredSignal();
        }

        public boolean hasPendingEvidenceCandidate() {
            return !pendingTypes.isEmpty();
        }

        public boolean hasDecisionContract() {
            for (TypeScore score : typeScores.values()) {
                if (score == null || score.decision == null) {
                    return false;
                }
            }
            return typeScores.size() == 7;
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
            if (hasPendingEvidenceCandidate()) {
                labels.add("待补证据：" + labels(pendingTypes) + "（不是买入信号）");
            }
            List<String> observedTypes = typesWithStatus("observe");
            if (!observedTypes.isEmpty()) {
                labels.add("观察：" + labels(observedTypes) + "（不是买入信号）");
            }
            List<String> insufficientTypes = typesWithStatus("insufficient_evidence");
            if (!insufficientTypes.isEmpty()) {
                labels.add("资料不足：" + labels(insufficientTypes) + "（不是买入信号）");
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
                if (score != null) {
                    for (String dimension : TYPE_DIMENSIONS.get(key)) {
                        output.append("\n  ")
                                .append(dimension)
                                .append(" ")
                                .append(DIMENSION_NAMES.getOrDefault(dimension, "子指标"))
                                .append("：")
                                .append(score.describeDimension(dimension));
                    }
                }
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
            int priority = (
                    hasTriggeredSignal()
                            ? 0
                            : hasConditionalCandidate()
                                    ? 1
                                    : hasPendingEvidenceCandidate() ? 2 : hasObservedFramework() ? 3 : 4
            );
            int otherPriority = (other.hasTriggeredSignal() ? 0
                    : other.hasConditionalCandidate()
                            ? 1
                            : other.hasPendingEvidenceCandidate() ? 2 : other.hasObservedFramework() ? 3 : 4);
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
        public final boolean potentiallyTriggerable;
        public final DecisionSummary decision;
        public final Map<String, Double> subScores;
        public final Map<String, String> subScoreReasons;
        public final Set<String> investorActionDimensions;

        TypeScore(String status, Double score, String reason) {
            this(status, score, reason, null, Collections.emptyMap(), Collections.emptyMap());
        }

        TypeScore(String status, Double score, String reason, DecisionSummary decision) {
            this(status, score, reason, decision, Collections.emptyMap(), Collections.emptyMap());
        }

        TypeScore(
                String status,
                Double score,
                String reason,
                DecisionSummary decision,
                Map<String, Double> subScores,
                Map<String, String> subScoreReasons
        ) {
            this(
                    status,
                    score,
                    reason,
                    decision,
                    subScores,
                    subScoreReasons,
                    Collections.emptySet()
            );
        }

        TypeScore(
                String status,
                Double score,
                String reason,
                DecisionSummary decision,
                Map<String, Double> subScores,
                Map<String, String> subScoreReasons,
                Set<String> investorActionDimensions
        ) {
            this.status = status;
            // Older signed server generations may still contain the scorer's
            // internal 0.0/0.9 placeholders.  Applicability and evidence state
            // are authoritative: neither state has a user-facing numeric score.
            this.score = isScorelessTypeStatus(status) ? null : score;
            this.reason = publicReasonText(reason);
            this.decision = decision;
            this.potentiallyTriggerable = decision != null && decision.potentiallyTriggerable;
            this.subScores = Collections.unmodifiableMap(new HashMap<>(subScores));
            this.subScoreReasons = Collections.unmodifiableMap(new HashMap<>(subScoreReasons));
            this.investorActionDimensions = Collections.unmodifiableSet(new HashSet<>(investorActionDimensions));
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
                    : text + "，" + publicScoreText(score) + "分";
            return reason == null || reason.isEmpty() ? scored : scored + "；" + reason;
        }

        String describeDimension(String dimension) {
            if ("not_applicable".equals(status)) {
                return "不适用";
            }
            if (investorActionDimensions.contains(dimension)) {
                boolean liveConfirmation = "conditional".equals(status)
                        && decision != null
                        && !decision.complete
                        && decision.potentiallyTriggerable;
                return liveConfirmation
                        ? "待确认仓位（投资者动作，不是公司资料缺失）"
                        : "当前无需确认仓位（投资者动作，不是公司资料缺失）";
            }
            Double value = subScores.get(dimension);
            if (value == null) {
                if (decision != null && decision.missingDimensions.contains(dimension)) {
                    return "资料不足（该项尚缺可核验数据）";
                }
                return "数据版本过旧，请获取最新数据";
            }
            String scored = publicScoreText(value) + "分";
            String evidence = subScoreReasons.get(dimension);
            return evidence == null || evidence.isEmpty() ? scored : scored + "；" + evidence;
        }
    }

    public static final class DecisionSummary {
        public final boolean complete;
        public final String basis;
        public final double lowerBound;
        public final double upperBound;
        public final String vetoState;
        public final boolean potentiallyTriggerable;
        public final List<String> missingDimensions;

        DecisionSummary(
                boolean complete,
                String basis,
                double lowerBound,
                double upperBound,
                String vetoState,
                boolean potentiallyTriggerable,
                List<String> missingDimensions
        ) {
            this.complete = complete;
            this.basis = basis;
            this.lowerBound = lowerBound;
            this.upperBound = upperBound;
            this.vetoState = vetoState;
            this.potentiallyTriggerable = potentiallyTriggerable;
            this.missingDimensions = Collections.unmodifiableList(new ArrayList<>(missingDimensions));
        }
    }
}
