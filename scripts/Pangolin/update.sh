#!/usr/bin/env bash
set -euo pipefail

TRAEFIK_CONFIG="./config/traefik/traefik_config.yml"
COMPOSE_FILE="./docker-compose.yml"

for cmd in curl jq yq docker; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "❌ '$cmd' fehlt."
        exit 1
    }
done

[[ -f "$COMPOSE_FILE" ]] || {
    echo "❌ $COMPOSE_FILE wurde nicht gefunden."
    exit 1
}

# Die Traefik-Konfiguration darf fehlen, wenn keine Plugins verwendet werden.
traefik_config_exists=false
if [[ -f "$TRAEFIK_CONFIG" ]]; then
    traefik_config_exists=true
fi

TIMESTAMP=$(date +%F_%H-%M-%S)
BACKUP_DIR="./backups"
YAML_BACKUP_DIR="$BACKUP_DIR"
CONFIG_BACKUP_DIR="$BACKUP_DIR"
BACKUP_RETENTION_DAYS=30
BACKUP_MIN_KEEP=3

mkdir -p "$YAML_BACKUP_DIR"

TRAEFIK_BACKUP="$YAML_BACKUP_DIR/traefik_config_${TIMESTAMP}.yml"
COMPOSE_BACKUP="$YAML_BACKUP_DIR/docker-compose_${TIMESTAMP}.yml"

if [[ "$traefik_config_exists" == true ]]; then
    cp -- "$TRAEFIK_CONFIG" "$TRAEFIK_BACKUP"
fi
cp -- "$COMPOSE_FILE" "$COMPOSE_BACKUP"
echo "📦 YAML-Backups erstellt: $YAML_BACKUP_DIR"

traefik_changed=false
compose_changed=false

declare -A ITEMS_ALT=()
declare -A ITEMS_NEW=()
declare -A ITEMS_TAGS=()
declare -A ITEMS_TYPE=()
declare -A ITEMS_PATH=()

###############################################################
# Hilfsfunktionen
###############################################################

# Erlaubt Tags wie 1, 1.2, 1.2.3 sowie Präfixe wie v oder ee-.
semver_filter() {
    local prefix="$1"
    local tags="$2"

    printf '%s\n' "$tags" \
        | grep -E "^${prefix}[0-9]+(\.[0-9]+){0,2}$" \
        || true
}

get_prefix() {
    local version="$1"
    printf '%s\n' "$version" | sed -E 's/^([^0-9]*).*/\1/'
}

# Liefert Repository und Tag eines Images.
# Images ohne expliziten Tag sowie Digest-Referenzen werden übersprungen.
parse_image() {
    local image="$1"

    if [[ "$image" == *@* ]]; then
        return 1
    fi

    local last_part="${image##*/}"
    if [[ "$last_part" != *:* ]]; then
        return 1
    fi

    IMAGE_REPO="${image%:*}"
    IMAGE_TAG="${image##*:}"

    [[ -n "$IMAGE_REPO" && -n "$IMAGE_TAG" ]]
}

fetch_github_tags() {
    local repo="$1"
    local response

    if ! response=$(curl -fsSL \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/${repo}/tags?per_page=100"); then
        return 1
    fi

    printf '%s\n' "$response" | jq -r '.[].name'
}

fetch_dockerhub_tags() {
    local repository="$1"
    local url="https://hub.docker.com/v2/repositories/${repository}/tags?page_size=100"
    local response
    local collected=""

    while [[ -n "$url" && "$url" != "null" ]]; do
        if ! response=$(curl -fsSL "$url"); then
            return 1
        fi

        collected+=$'\n'$(printf '%s\n' "$response" | jq -r '.results[]?.name')
        url=$(printf '%s\n' "$response" | jq -r '.next // empty')
    done

    printf '%s\n' "$collected" | sed '/^$/d'
}

fetch_ghcr_tags() {
    local repository="$1"
    local token
    local response

    if ! token=$(curl -fsSL \
        "https://ghcr.io/token?scope=repository:${repository}:pull" \
        | jq -r '.token // empty'); then
        return 1
    fi

    [[ -n "$token" ]] || return 1

    if ! response=$(curl -fsSL \
        -H "Authorization: Bearer $token" \
        "https://ghcr.io/v2/${repository}/tags/list?n=10000"); then
        return 1
    fi

    printf '%s\n' "$response" | jq -r '.tags[]?'
}

register_item() {
    local key="$1"
    local current="$2"
    local latest="$3"
    local tags="$4"
    local type="$5"
    local path="$6"

    ITEMS_ALT["$key"]="$current"
    ITEMS_NEW["$key"]="$latest"
    ITEMS_TAGS["$key"]="$tags"
    ITEMS_TYPE["$key"]="$type"
    ITEMS_PATH["$key"]="$path"
}

# Löscht Backups erst, wenn sie älter als BACKUP_RETENTION_DAYS sind.
# Unabhängig vom Alter bleiben pro Muster immer mindestens die neuesten
# BACKUP_MIN_KEEP Dateien erhalten.
cleanup_old_backups() {
    local directory="$1"
    local pattern="$2"
    local label="$3"

    [[ -d "$directory" ]] || return 0

    local -a files=()
    mapfile -t files < <(
        find "$directory" -maxdepth 1 -type f -name "$pattern" \
            -printf '%T@ %p\n' \
            | sort -nr \
            | cut -d' ' -f2-
    )

    local index file deleted=0
    for index in "${!files[@]}"; do
        # Die drei neuesten Backups bleiben immer erhalten.
        (( index < BACKUP_MIN_KEEP )) && continue

        file="${files[$index]}"
        if find "$file" -maxdepth 0 -type f -mtime "+${BACKUP_RETENTION_DAYS}" -print -quit \
            | grep -q .; then
            rm -f -- "$file"
            echo "🗑 Altes $label-Backup gelöscht: $file"
            ((deleted += 1))
        fi
    done

    if (( deleted == 0 )); then
        echo "ℹ️ Keine alten $label-Backups zu löschen (mindestens $BACKUP_MIN_KEEP bleiben erhalten)."
    fi
}

###############################################################
# 1️⃣ Optionale Traefik-Plugins prüfen
###############################################################

plugins=""
if [[ "$traefik_config_exists" == true ]]; then
    plugins=$(yq -r '(.experimental.plugins // {}) | keys | .[]' "$TRAEFIK_CONFIG")
fi

if [[ -z "$plugins" ]]; then
    echo "ℹ️ Keine Traefik-Plugins konfiguriert – übersprungen."
else
    while IFS= read -r plugin; do
        [[ -n "$plugin" ]] || continue

        module=$(yq -r ".experimental.plugins[\"$plugin\"].moduleName // empty" "$TRAEFIK_CONFIG")
        current=$(yq -r ".experimental.plugins[\"$plugin\"].version // empty" "$TRAEFIK_CONFIG")

        if [[ -z "$module" || -z "$current" ]]; then
            echo "⚠️ Plugin '$plugin' hat keine gültige moduleName/version – übersprungen."
            continue
        fi

        repo="${module#github.com/}"

        if ! all_tags=$(fetch_github_tags "$repo"); then
            echo "⚠️ Tags für Plugin '$plugin' konnten nicht geladen werden – übersprungen."
            continue
        fi

        prefix=$(get_prefix "$current")
        valid=$(semver_filter "$prefix" "$all_tags")
        latest=$(printf '%s\n' "$valid" | sort -Vr | head -n1)

        register_item \
            "plugin:$plugin" "$current" "$latest" "$valid" \
            "plugin" "$plugin"
    done <<< "$plugins"
fi

###############################################################
# 2️⃣ Vorhandene Docker-Compose-Container prüfen
###############################################################

# Nur tatsächlich vorhandene Services werden eingelesen.
# Optionale Container wie GeoIPUpdate oder CrowdSec dürfen fehlen.
images=$(yq -r '.services // {} | .[] | .image? // empty' "$COMPOSE_FILE")

if [[ -z "$images" ]]; then
    echo "ℹ️ Keine Images in docker-compose.yml gefunden."
else
    while IFS= read -r image; do
        [[ -n "$image" ]] || continue

        if ! parse_image "$image"; then
            echo "⚠️ Image '$image' hat keinen auswertbaren Tag oder nutzt einen Digest – übersprungen."
            continue
        fi

        repo="$IMAGE_REPO"
        current="$IMAGE_TAG"
        all_tags=""

        if [[ "$repo" == docker.io/* ]]; then
            repo_clean="${repo#docker.io/}"

            # Offizielle Docker-Hub-Images liegen im Namespace library/.
            if [[ "$repo_clean" != */* ]]; then
                repo_clean="library/$repo_clean"
            fi

            if ! all_tags=$(fetch_dockerhub_tags "$repo_clean"); then
                echo "⚠️ Docker-Hub-Tags für '$image' konnten nicht geladen werden – übersprungen."
                continue
            fi

        elif [[ "$repo" == ghcr.io/* ]]; then
            repo_clean="${repo#ghcr.io/}"

            if ! all_tags=$(fetch_ghcr_tags "$repo_clean"); then
                echo "⚠️ GHCR-Tags für '$image' konnten nicht geladen werden – übersprungen."
                continue
            fi

        else
            echo "⚠️ Registry von '$image' wird nicht unterstützt – übersprungen."
            continue
        fi

        prefix=$(get_prefix "$current")
        valid=$(semver_filter "$prefix" "$all_tags")
        latest=$(printf '%s\n' "$valid" | sort -Vr | head -n1)

        register_item \
            "image:$image" "$current" "$latest" "$valid" \
            "image" "$image"
    done <<< "$images"
fi

###############################################################
# 3️⃣ Gesamtübersicht
###############################################################

echo ""
echo "=============================="
echo "📋 GESAMT-ÜBERSICHT"
echo "=============================="

updates_available=false

if [[ ${#ITEMS_ALT[@]} -eq 0 ]]; then
    echo "ℹ️ Keine auswertbaren Plugins oder Container gefunden."
else
    while IFS= read -r key; do
        alt="${ITEMS_ALT[$key]}"
        neu="${ITEMS_NEW[$key]}"

        if [[ -z "$neu" ]]; then
            status="❌ keine gültigen semver-Tags"
        elif [[ "$alt" == "$neu" ]]; then
            status="✅ aktuell"
        else
            status="🔄 Update möglich"
            updates_available=true
        fi

        printf "%-50s | ALT: %-14s | NEU: %-14s | %s\n" \
            "$key" "$alt" "$neu" "$status"
    done < <(printf '%s\n' "${!ITEMS_ALT[@]}" | sort)
fi

###############################################################
# 4️⃣ Updates automatisch oder manuell übernehmen
###############################################################

apply_update() {
    local key="$1"
    local new_tag="$2"
    local type="${ITEMS_TYPE[$key]}"
    local path="${ITEMS_PATH[$key]}"

    if [[ "$type" == "plugin" ]]; then
        yq -i -Y \
            ".experimental.plugins[\"$path\"].version = \"$new_tag\"" \
            "$TRAEFIK_CONFIG"
        traefik_changed=true
        echo "   🔧 Plugin $path → $new_tag"
    else
        local repo_img="${path%:*}"
        yq -i -Y \
            "(.services[] | select(.image == \"$path\") | .image) = \"$repo_img:$new_tag\"" \
            "$COMPOSE_FILE"
        compose_changed=true
        echo "   🐳 Image $path → $new_tag"
    fi
}

if [[ "$updates_available" == true ]]; then
    echo ""
    read -r -p "Updates wie angezeigt automatisch übernehmen? (y/N): " auto

    if [[ "$auto" =~ ^[Yy]$ ]]; then
        echo ""
        echo "🚀 Automatische Updates werden übernommen …"

        while IFS= read -r key; do
            alt="${ITEMS_ALT[$key]}"
            neu="${ITEMS_NEW[$key]}"

            [[ -n "$neu" && "$alt" != "$neu" ]] || continue
            apply_update "$key" "$neu"
        done < <(printf '%s\n' "${!ITEMS_ALT[@]}" | sort)

    else
        echo ""
        read -r -p "Manuelle Auswahl starten? (y/N): " manual

        if [[ "$manual" =~ ^[Yy]$ ]]; then
            echo ""
            echo "=============================="
            echo "🎛 MANUELLE AUSWAHL"
            echo "=============================="

            while IFS= read -r key; do
                alt="${ITEMS_ALT[$key]}"
                neu="${ITEMS_NEW[$key]}"

                [[ -n "$neu" && "$alt" != "$neu" ]] || continue

                echo ""
                echo "➡️ $key"
                echo "   ALT: $alt"
                echo "   NEU: $neu"
                echo "   📜 Verfügbare semver-Tags:"

                mapfile -t tag_list < <(printf '%s\n' "${ITEMS_TAGS[$key]}" | sort -Vr)

                for i in "${!tag_list[@]}"; do
                    printf "      [%d] %s\n" "$((i + 1))" "${tag_list[$i]}"
                done
                echo "      [0] nicht aktualisieren"

                read -r -p "   Auswahl: " choice

                if [[ "$choice" == "0" ]]; then
                    echo "   ⏸ übersprungen"
                    continue
                fi

                if [[ ! "$choice" =~ ^[0-9]+$ ]] \
                    || (( choice < 1 || choice > ${#tag_list[@]} )); then
                    echo "   ⚠️ Ungültige Auswahl – übersprungen."
                    continue
                fi

                apply_update "$key" "${tag_list[$((choice - 1))]}"
            done < <(printf '%s\n' "${!ITEMS_ALT[@]}" | sort)
        fi
    fi
else
    echo ""
    echo "✅ Keine Updates verfügbar."
fi

###############################################################
# 5️⃣ Nicht benötigte Backups löschen
###############################################################

if [[ "$traefik_config_exists" == true && "$traefik_changed" == false ]]; then
    rm -f "$TRAEFIK_BACKUP"
    echo "🗑 Traefik-Backup gelöscht, da keine Änderung vorgenommen wurde."
fi

if [[ "$compose_changed" == false ]]; then
    rm -f "$COMPOSE_BACKUP"
    echo "🗑 docker-compose-Backup gelöscht, da keine Änderung vorgenommen wurde."
fi

###############################################################
# 6️⃣ Backup-Aufbewahrung anwenden
###############################################################

cleanup_old_backups "$YAML_BACKUP_DIR" "docker-compose_*.yml" "docker-compose"
cleanup_old_backups "$YAML_BACKUP_DIR" "traefik_config_*.yml" "Traefik"

###############################################################
# 7️⃣ Docker Compose aktualisieren
###############################################################

echo ""
read -r -p "🚀 docker-compose jetzt aktualisieren (pull + up -d)? (y/N): " ans

if [[ "$ans" =~ ^[Yy]$ ]]; then
    if [[ -d "./config" ]]; then
        echo ""
        read -r -p "📦 Vor dem Container-Update ein Backup von ./config erstellen? (y/N): " config_backup

        if [[ "$config_backup" =~ ^[Yy]$ ]]; then
            mkdir -p "$CONFIG_BACKUP_DIR"
            CONFIG_BACKUP_FILE="$CONFIG_BACKUP_DIR/config_${TIMESTAMP}.tar.gz"

            if tar -czf "$CONFIG_BACKUP_FILE" ./config; then
                echo "✅ Config-Backup erstellt: $CONFIG_BACKUP_FILE"
            else
                echo "❌ Config-Backup fehlgeschlagen. Container-Update wird abgebrochen."
                exit 1
            fi
        else
            echo "⏸ Config-Backup übersprungen."
        fi
    else
        echo "ℹ️ ./config existiert nicht – Config-Backup übersprungen."
    fi

    cleanup_old_backups "$CONFIG_BACKUP_DIR" "config_*.tar.gz" "Config"

    docker compose -f "$COMPOSE_FILE" pull
    docker compose -f "$COMPOSE_FILE" up -d
    echo "🎉 docker-compose erfolgreich aktualisiert."
else
    echo "⏸ Aktualisierung übersprungen."
    cleanup_old_backups "$CONFIG_BACKUP_DIR" "config_*.tar.gz" "Config"
fi
