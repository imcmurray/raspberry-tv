//! Handles all interactions with the CouchDB database.
//!
//! This module provides functions for fetching slideshow documents, attachments,
//! updating TV status, watching for changes, and cleaning up unused attachments.
//! All functions are asynchronous and use the `reqwest` client for HTTP communication
//! and custom error types defined in `src/errors.rs`.

use super::config::AppConfig;
use super::model::{Slide, SlideshowDocument};
use super::errors::CouchDbError;
use reqwest::Client;
use std::collections::{HashMap, HashSet};
use log::{info, error, warn, debug, trace}; // Added trace
use tokio_stream::StreamExt;
use reqwest::header::ACCEPT;
use std::time::Duration;
use tempfile::NamedTempFile;
use serde_json; // For JSON payloads and parsing RevResponse
use std::sync::Arc;
use std::sync::Mutex;

/// Extracts a set of all attachment keys referenced by slides in a document.
///
/// This is used by the attachment cleanup process to determine which attachments
/// are actively in use.
pub fn get_referenced_attachments_from_doc(doc: &SlideshowDocument) -> HashSet<String> {
    trace!("Getting referenced attachments from document ID: {}", doc._id);
    let mut referenced = HashSet::new();
    if let Some(slides) = &doc.slides {
        for slide in slides {
            match slide.type_.to_lowercase().as_str() {
                "image" | "picture" | "video" => {
                    let key = slide.filename.as_ref().filter(|s| !s.is_empty()).unwrap_or(&slide.name).clone();
                    trace!("Found referenced attachment key: '{}' in slide: '{}'", key, slide.name);
                    referenced.insert(key);
                }
                _ => {} // Other slide types (e.g., website) don't have CouchDB attachments here.
            }
        }
    }
    debug!("Total referenced attachments for doc ID {}: {}", doc._id, referenced.len());
    referenced
}

/// Fetches the main slideshow document from CouchDB, including attachment stubs.
#[must_use = "fetching the document can fail; the Result must be handled"]
pub async fn fetch_document_with_attachments(config: &AppConfig, client: &Client) -> Result<SlideshowDocument, CouchDbError> {
    debug!("Fetching slideshow document for tv_uuid: {}", config.tv_uuid);
    let url = format!("{}/slideshows/{}", config.couchdb_url, config.tv_uuid);
    let response = client.get(&url).send().await.map_err(CouchDbError::Reqwest)?;

    if response.status() == reqwest::StatusCode::NOT_FOUND {
        warn!("Slideshow document not found for tv_uuid: {} at URL: {}", config.tv_uuid, url);
        return Err(CouchDbError::NotFound(format!("Slideshow document for tv_uuid '{}'", config.tv_uuid)));
    }
    let response = response.error_for_status().map_err(|e| {
        let status = e.status().unwrap_or(reqwest::StatusCode::INTERNAL_SERVER_ERROR);
        error!("HTTP error fetching slideshow document for tv_uuid {}: {} - {}", config.tv_uuid, status, e);
        CouchDbError::HttpError { status, message: e.to_string() }
    })?;

    let doc = response.json::<SlideshowDocument>().await.map_err(|e| {
        error!("Failed to parse SlideshowDocument for tv_uuid {}: {:?}", config.tv_uuid, e);
        CouchDbError::from(e) // Let From trait for reqwest::Error handle it.
    })?;
    info!("Successfully fetched slideshow document for tv_uuid: {}, _rev: {}", config.tv_uuid, doc._rev);
    Ok(doc)
}

/// Fetches a CouchDB attachment and streams it to a temporary file.
#[must_use = "fetching an attachment can fail; the Result must be handled"]
pub async fn fetch_attachment_to_temp_file(config: &AppConfig, client: &Client, attachment_name: &str) -> Result<NamedTempFile, CouchDbError> {
    debug!("Fetching attachment '{}' to temp file for tv_uuid: {}", attachment_name, config.tv_uuid);
    let url = format!("{}/slideshows/{}/{}", config.couchdb_url, config.tv_uuid, attachment_name);
    let response = client.get(&url).send().await.map_err(|e| {
        error!("Request error fetching attachment '{}' for tv_uuid {}: {:?}", attachment_name, config.tv_uuid, e);
        CouchDbError::Reqwest(e)
    })?;
    let response = response.error_for_status().map_err(|e| {
        let status = e.status().unwrap_or(reqwest::StatusCode::INTERNAL_SERVER_ERROR);
        error!("HTTP error fetching attachment '{}' for tv_uuid {}: {} - {}", attachment_name, config.tv_uuid, status, e);
        CouchDbError::HttpError { status, message: e.to_string() }
    })?;

    let mut temp_file = NamedTempFile::new().map_err(|e| {
        error!("Failed to create temp file for attachment '{}': {:?}", attachment_name, e);
        CouchDbError::Generic(format!("Failed to create temp file for '{}': {}", attachment_name, e))
    })?;
    trace!("Created temp file for attachment '{}' at: {:?}", attachment_name, temp_file.path());
    let mut stream = response.bytes_stream();

    while let Some(item) = stream.next().await {
        let chunk = item.map_err(|e| {
            error!("Stream error while downloading attachment '{}' for tv_uuid {}: {:?}", attachment_name, config.tv_uuid, e);
            CouchDbError::Reqwest(e)
        })?;
        std::io::Write::write_all(&mut temp_file, &chunk).map_err(|e| {
            error!("Failed to write chunk to temp file for attachment '{}': {:?}", attachment_name, e);
            CouchDbError::Generic(format!("Failed to write chunk for '{}': {}", attachment_name, e))
        })?;
    }
    temp_file.flush().map_err(|e| {
        error!("Failed to flush temp file for attachment '{}': {:?}", attachment_name, e);
        CouchDbError::Generic(format!("Failed to flush temp file for '{}': {}", attachment_name, e))
    })?;
    info!("Successfully fetched attachment '{}' to temp file ({:?}) for tv_uuid: {}", attachment_name, temp_file.path(), config.tv_uuid);
    Ok(temp_file)
}

/// Helper struct for parsing `_rev` from CouchDB update/delete responses.
#[derive(Deserialize, Debug)]
struct RevResponse { _rev: String }

/// Updates the TV status document in CouchDB.
#[must_use = "updating TV status can fail; the Result must be handled"]
pub async fn update_tv_status(config: &AppConfig, client: &Client, current_slide_id: &str, current_slide_filename: &str) -> Result<(), CouchDbError> {
    debug!("Updating TV status for tv_uuid: {}, current_slide_id: '{}', filename: '{}'", config.tv_uuid, current_slide_id, current_slide_filename);
    let status_doc_id = format!("status_{}", config.tv_uuid);
    let status_url = format!("{}/slideshows/{}", config.couchdb_url, status_doc_id);

    let mut current_rev = None;
    trace!("Fetching current _rev for status document: {}", status_doc_id);
    match client.get(&status_url).send().await {
        Ok(resp) => {
            if resp.status() == reqwest::StatusCode::OK {
                match resp.json::<RevResponse>().await { // map_err(CouchDbError::Reqwest) is fine too
                    Ok(rev_resp) => {
                        debug!("Found existing status doc '{}' with _rev: {}", status_doc_id, rev_resp._rev);
                        current_rev = Some(rev_resp._rev);
                    }
                    Err(e) => {
                        warn!("Failed to parse _rev from status doc '{}', proceeding without _rev. Error: {}", status_doc_id, e);
                    }
                }
            } else if resp.status() == reqwest::StatusCode::NOT_FOUND {
                debug!("Status doc '{}' not found, will create new.", status_doc_id);
            } else {
                let err_msg = format!("Failed to fetch status doc '{}', status: {}", status_doc_id, resp.status());
                error!("{}", err_msg);
                return Err(CouchDbError::HttpError{ status: resp.status(), message: err_msg});
            }
        }
        Err(e) => {
            error!("Request error fetching status doc '{}': {:?}", status_doc_id, e);
            return Err(CouchDbError::Reqwest(e));
        }
    }

    let mut payload = serde_json::json!({
        "type": "tv_status",
        "tv_uuid": config.tv_uuid,
        "current_slide_id": current_slide_id,
        "current_slide_filename": current_slide_filename,
        "timestamp": chrono::Utc::now().to_rfc3339(),
    });
    if let Some(rev) = current_rev {
        payload["_rev"] = serde_json::Value::String(rev);
    }
    trace!("TV status payload for doc '{}': {}", status_doc_id, payload);

    let response = client.put(&status_url).json(&payload).send().await.map_err(|e| {
        error!("Request error updating TV status for tv_uuid {}: {:?}", config.tv_uuid, e);
        CouchDbError::Reqwest(e)
    })?;

    if response.status().is_success() {
        info!("Successfully updated TV status for tv_uuid: {}", config.tv_uuid);
        Ok(())
    } else {
        let status = response.status();
        let error_message = response.text().await.unwrap_or_else(|e| format!("N/A (failed to read error body: {})", e));
        error!("Failed to update TV status for tv_uuid {}. Status: {}, Body: {}", config.tv_uuid, status, error_message);
        Err(CouchDbError::HttpError{ status, message: format!("Failed to update TV status for {}: {}", config.tv_uuid, error_message) })
    }
}

/// Watches the CouchDB `_changes` feed for the TV's slideshow document.
///
/// This function runs in a continuous loop, attempting to reconnect on errors.
/// When a change is detected, it sets a flag and requests the UI to repaint.
pub async fn watch_couchdb_changes(config: AppConfig, needs_refetch_flag: Arc<Mutex<bool>>, client: Client, ctx: egui::Context) {
    let url = format!(
        "{}/slideshows/_changes?feed=continuous&heartbeat=10000&doc_ids=[\"{}\"]&since=now",
        config.couchdb_url, config.tv_uuid
    );
    info!("Starting CouchDB changes feed listener for tv_uuid: {} at URL: {}", config.tv_uuid, url);
    loop {
        let response_result = client.get(&url)
            .header(ACCEPT, "application/json")
            .send()
            .await;

        match response_result {
            Ok(response) => {
                if response.status().is_success() {
                    let mut stream = response.bytes_stream();
                    info!("Successfully connected to CouchDB changes feed for tv_uuid: {}", config.tv_uuid);
                    while let Some(item) = stream.next().await {
                        match item {
                            Ok(bytes) => {
                                if bytes.is_empty() || bytes.as_ref() == b"\n" {
                                    trace!("Changes feed heartbeat or empty line for tv_uuid: {}.", config.tv_uuid);
                                    continue;
                                }
                                if let Ok(json_val) = serde_json::from_slice::<serde_json::Value>(&bytes) {
                                    if json_val.get("id").is_some() && json_val.get("seq").is_some() {
                                        info!("Change detected for tv_uuid {}. Sequence: {:?}. Triggering refetch.", config.tv_uuid, json_val.get("seq"));
                                        *needs_refetch_flag.lock().unwrap() = true;
                                        ctx.request_repaint();
                                    } else {
                                        debug!("Received non-change JSON object from CouchDB feed for tv_uuid {}: {}", config.tv_uuid, String::from_utf8_lossy(&bytes));
                                    }
                                } else {
                                     debug!("Received non-JSON line from CouchDB feed for tv_uuid {} (may be OK): {}", config.tv_uuid, String::from_utf8_lossy(&bytes));
                                }
                            }
                            Err(e) => {
                                error!("Error reading from CouchDB changes stream for tv_uuid {}: {}", config.tv_uuid, e);
                                break;
                            }
                        }
                    }
                } else {
                    error!("CouchDB changes feed request for tv_uuid {} failed with status: {}", config.tv_uuid, response.status());
                }
            }
            Err(e) => {
                error!("Failed to connect to CouchDB changes feed for tv_uuid {}: {}", config.tv_uuid, e);
            }
        }
        warn!("Changes feed for tv_uuid {} disconnected or errored. Attempting to reconnect in 10 seconds...", config.tv_uuid);
        tokio::time::sleep(Duration::from_secs(10)).await;
    }
}

/// Performs cleanup of unused attachments in a CouchDB document, using a provided document.
#[must_use = "attachment cleanup can fail; the Result must be handled"]
pub async fn perform_attachment_cleanup_with_doc(
    config: &AppConfig,
    client: &Client,
    doc: SlideshowDocument,
    immediate: bool
) -> Result<(), CouchDbError> {
    debug!("Starting attachment cleanup (immediate: {}) for doc ID: {}", immediate, doc._id);
    let referenced_attachments = get_referenced_attachments_from_doc(&doc);
    trace!("Referenced attachments for doc {}: {:?}", doc._id, referenced_attachments);

    let actual_attachments = match &doc._attachments {
        Some(attachments_map) => attachments_map.keys().cloned().collect::<HashSet<String>>(),
        None => { info!("No _attachments field found in doc {}. No attachments to clean.", doc._id); return Ok(()); }
    };
    trace!("Actual attachments found in doc {}: {:?}", doc._id, actual_attachments);

    let unused_attachments: Vec<String> = actual_attachments.difference(&referenced_attachments).cloned().collect();

    if unused_attachments.is_empty() { info!("No unused attachments found for document {}", doc._id); return Ok(()); }
    info!("Found {} unused attachments for doc {}: {:?}", unused_attachments.len(), doc._id, unused_attachments);
    let mut current_rev = doc._rev.clone();

    for attachment_name in unused_attachments {
        if !immediate {
             match fetch_document_with_attachments(config, client).await {
                Ok(latest_doc) => {
                    debug!("Periodic cleanup: Fetched latest doc rev {} before deleting attachment {} for doc {}", latest_doc._rev, attachment_name, doc._id);
                    current_rev = latest_doc._rev
                },
                Err(e) => {
                    error!("Periodic cleanup: Failed to fetch latest doc for revision before deleting attachment {} for doc {}: {}", attachment_name, doc._id, e);
                    return Err(CouchDbError::Generic(format!("Failed to fetch latest doc rev before deleting {}: {}", attachment_name, e)));
                }
            }
        }
        let delete_url = format!("{}/slideshows/{}/{}?rev={}", config.couchdb_url, config.tv_uuid, attachment_name, current_rev);
        debug!("Attempting to delete attachment: {} from doc {} with rev {}", attachment_name, doc._id, current_rev);

        let response = client.delete(&delete_url).send().await.map_err(CouchDbError::Reqwest)?;
        if response.status().is_success() {
            info!("Successfully deleted attachment: {} from doc {}", attachment_name, doc._id);
            if let Ok(json_resp) = response.json::<serde_json::Value>().await {
                if let Some(new_rev) = json_resp.get("rev").and_then(|v| v.as_str()) {
                    debug!("Doc {} new revision after deleting attachment {}: {}", doc._id, attachment_name, new_rev);
                    current_rev = new_rev.to_string();
                }
            }
        } else {
            let status = response.status();
            let err_body = response.text().await.unwrap_or_else(|e| format!("N/A (failed to read body: {})", e));
            error!("Failed to delete attachment {} from doc {}. Status: {}, Body: {}", attachment_name, doc._id, status, err_body);
            return Err(CouchDbError::HttpError{ status, message: format!("Failed to delete attachment {}", attachment_name) });
        }
        if !immediate {
            debug!("Periodic cleanup: Pausing briefly after deleting attachment {} for doc {}...", attachment_name, doc._id);
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
    info!("Attachment cleanup finished for doc {}", doc._id);
    Ok(())
}

/// Fetches the slideshow document and then performs cleanup of unused attachments.
#[must_use = "attachment cleanup can fail; the Result must be handled"]
pub async fn perform_attachment_cleanup(config: &AppConfig, client: &Client, immediate: bool) -> Result<(), CouchDbError> {
    debug!("Performing attachment cleanup (immediate: {}), for tv_uuid: {}", immediate, config.tv_uuid);
    match fetch_document_with_attachments(config, client).await {
        Ok(doc) => {
            debug!("Document fetched for cleanup for tv_uuid: {}. Starting cleanup process.", config.tv_uuid);
            perform_attachment_cleanup_with_doc(config, client, doc, immediate).await
        }
        Err(e) => {
            error!("Failed to fetch document for cleanup for tv_uuid {}: {}", config.tv_uuid, e);
            Err(e)
        }
    }
}
