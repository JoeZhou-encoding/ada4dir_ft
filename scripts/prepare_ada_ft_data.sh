#!/bin/bash
# Unzip and arrange the MDRS ada_ft dataset into the structure PairLoader expects.
#
# Input zips (already placed in ${ADA_FT}):
#   train single-degradation subsets: blur.zip clean.zip noise.zip haze.zip dark.zip
#       each contains ONE top-level folder named after the type, e.g. blur/101.png
#   val.zip / test.zip: each contains a top-level folder (val/ or test/) with
#       per-degradation subfolders (blur/, noise/, ..., clean/)
#
# Output layout:
#   ${ADA_FT}/train/{clean,blur,noise,haze,dark}/*.png
#   ${ADA_FT}/val/{clean,blur,...}/*.png
#   ${ADA_FT}/test/{clean,blur,...}/*.png
#
# train_single.py / eval_single.py then use:
#   --data_dir ${REPO}/data --train_set ada_ft/train --val_set ada_ft/val
#   (eval test set: ada_ft/test)
set -euo pipefail

REPO="${REPO:-/home/pj24003162/ku40003404/weihao/05/Ada4DIR}"
ADA_FT="${ADA_FT:-${REPO}/data/ada_ft}"

cd "${ADA_FT}"
echo "[prepare] working dir: ${ADA_FT}"
echo "[prepare] zips present:"
ls -la ./*.zip || { echo "[prepare] ERROR: no zips found in ${ADA_FT}"; exit 1; }

# ---------- 1) train single-degradation subsets ----------
mkdir -p train
for name in clean blur noise haze dark; do
    if [ ! -f "${name}.zip" ]; then
        echo "[prepare] WARN: ${name}.zip missing, skipping"
        continue
    fi
    echo "[prepare] unzip ${name}.zip -> train/${name}/"
    rm -rf "train/${name}" "tmp_${name}"
    unzip -q -o "${name}.zip" -d "tmp_${name}"
    if [ -d "tmp_${name}/${name}" ]; then
        mv "tmp_${name}/${name}" "train/${name}"
    else
        # fallback: pngs not wrapped in a <name>/ folder
        mkdir -p "train/${name}"
        find "tmp_${name}" -type f -name '*.png' -exec mv {} "train/${name}/" \;
    fi
    rm -rf "tmp_${name}"
done

# ---------- 2) val / test (already structured with subfolders) ----------
for split in val test; do
    if [ ! -f "${split}.zip" ]; then
        echo "[prepare] WARN: ${split}.zip missing, skipping"
        continue
    fi
    echo "[prepare] unzip ${split}.zip -> ${split}/"
    rm -rf "${split}" "tmp_${split}"
    unzip -q -o "${split}.zip" -d "tmp_${split}"
    if [ -d "tmp_${split}/${split}" ]; then
        mv "tmp_${split}/${split}" "${split}"
    else
        mkdir -p "${split}"
        mv "tmp_${split}"/* "${split}/"
    fi
    rm -rf "tmp_${split}"
done

# ---------- 3) verify structure + counts ----------
echo ""
echo "===== STRUCTURE / COUNTS ====="
for split in train val test; do
    echo "--- ${split} ---"
    if [ ! -d "${split}" ]; then echo "  (missing ${split}/)"; continue; fi
    for sub in "${split}"/*/; do
        [ -d "${sub}" ] || continue
        sub_name=$(basename "${sub}")
        cnt=$(find "${sub}" -type f -name '*.png' | wc -l)
        printf "  %-8s %6d png\n" "${sub_name}" "${cnt}"
    done
    [ -d "${split}/clean" ] || echo "  WARNING: ${split}/clean missing -> cannot compute PSNR/SSIM for ${split}"
done

# ---------- 4) pairing check: every degradation must align 1:1 with clean ----------
echo ""
echo "===== PAIRING CHECK (degradation filenames vs clean) ====="
for split in train val test; do
    [ -d "${split}/clean" ] || continue
    for sub in "${split}"/*/; do
        sub_name=$(basename "${sub}")
        [ "${sub_name}" = "clean" ] && continue
        nclean=$(find "${split}/clean" -type f -name '*.png' | wc -l)
        nsub=$(find "${sub}" -type f -name '*.png' | wc -l)
        if diff <(cd "${split}/clean" && ls -- *.png | sort) \
                <(cd "${sub}" && ls -- *.png | sort) >/dev/null 2>&1; then
            printf "  %-5s %-8s clean=%d %s=%d  names ALIGNED OK\n" "${split}" "${sub_name}" "${nclean}" "${sub_name}" "${nsub}"
        else
            printf "  %-5s %-8s clean=%d %s=%d  NAME MISMATCH -> PairLoader pairing WRONG, inspect!\n" "${split}" "${sub_name}" "${nclean}" "${sub_name}" "${nsub}"
        fi
    done
done

echo ""
echo "[prepare] DONE."
echo "[prepare] train_single.py usage:"
echo "  --data_dir ${REPO}/data --train_set ada_ft/train --val_set ada_ft/val"
