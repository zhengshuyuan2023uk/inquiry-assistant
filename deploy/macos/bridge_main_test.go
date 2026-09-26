package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestControlRequiresSecretAndRejectsBrowserOrigins(t *testing.T) {
	stopped := false
	handler := deliveryHandler("secret-token", "127.0.0.1:12345", func() map[string]any { return map[string]any{"state": "waiting_login"} }, func() { stopped = true })
	for _, route := range []string{"/status", "/shutdown"} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:12345"+route, nil)
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		if out.Code != 401 || stopped {
			t.Fatalf("unauthenticated %s returned %d or stopped", route, out.Code)
		}
	}
	req := httptest.NewRequest("POST", "http://127.0.0.1:12345/shutdown", nil)
	req.Header.Set("Authorization", "Bearer secret-token")
	req.Header.Set("Origin", "https://outside.example")
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, req)
	if out.Code != 403 || stopped {
		t.Fatal("browser origin was accepted")
	}
}

func TestControlNeverExposesSendOrDownload(t *testing.T) {
	handler := deliveryHandler("secret-token", "127.0.0.1:12345", func() map[string]any { return map[string]any{"state": "connected"} }, func() { t.Fatal("unexpected stop") })
	for _, route := range []string{"/api/send", "/api/download", "/anything"} {
		req := httptest.NewRequest("POST", "http://127.0.0.1:12345"+route, strings.NewReader(`{"message":"MUST NOT SEND"}`))
		req.Header.Set("Authorization", "Bearer secret-token")
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		if out.Code != 404 {
			t.Fatalf("%s exposed: %d", route, out.Code)
		}
	}
}

func TestControlStatusAndStopHaveDistinctMethods(t *testing.T) {
	stopped := false
	handler := deliveryHandler("secret-token", "127.0.0.1:12345", func() map[string]any { return map[string]any{"state": "waiting_login", "instance_id": "synthetic"} }, func() { stopped = true })
	for _, example := range []struct {
		method, path string
		code         int
	}{{"GET", "/status", 200}, {"GET", "/shutdown", 405}, {"POST", "/status", 405}, {"POST", "/shutdown", 200}} {
		req := httptest.NewRequest(example.method, "http://127.0.0.1:12345"+example.path, nil)
		req.Header.Set("Authorization", "Bearer secret-token")
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		if out.Code != example.code {
			t.Fatalf("%s %s got %d", example.method, example.path, out.Code)
		}
		if example.path == "/status" && out.Code == http.StatusOK && !strings.Contains(out.Body.String(), `"waiting_login"`) {
			t.Fatal("status missing")
		}
	}
	if !stopped {
		t.Fatal("authenticated shutdown did not notify main")
	}
}

func TestCleanupPreservesSuccessorQRAndRecord(t *testing.T) {
	folder := t.TempDir()
	record := filepath.Join(folder, "runtime.json")
	qr := filepath.Join(folder, "login-qr.png")
	payload, _ := json.Marshal(map[string]string{"instance_id": "successor"})
	os.WriteFile(record, payload, 0600)
	os.WriteFile(qr, []byte("new QR"), 0600)
	deliveryCleanup(record, qr, "old-instance")
	if data, err := os.ReadFile(qr); err != nil || string(data) != "new QR" {
		t.Fatal("old instance removed successor QR")
	}
	if data, err := os.ReadFile(record); err != nil || string(data) != string(payload) {
		t.Fatal("old instance altered successor record")
	}
	deliveryCleanup(record, qr, "successor")
	if _, err := os.Stat(qr); !os.IsNotExist(err) {
		t.Fatal("own QR not cleaned")
	}
	if _, err := os.Stat(record); !os.IsNotExist(err) {
		t.Fatal("own runtime not cleaned")
	}
}
