// Packaged Inquiry Assistant entry point. The upstream REST API is not compiled.
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"rsc.io/qr"
)

func deliveryHandler(token, host string, snapshot func() map[string]any, stop func()) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Host != host || r.Header.Get("Origin") != "" {
			http.Error(w, "forbidden", 403)
			return
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) != 1 {
			http.Error(w, "unauthorized", 401)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/status":
			if r.Method != "GET" {
				http.Error(w, "method not allowed", 405)
				return
			}
			json.NewEncoder(w).Encode(snapshot())
		case "/shutdown":
			if r.Method != "POST" {
				http.Error(w, "method not allowed", 405)
				return
			}
			json.NewEncoder(w).Encode(map[string]bool{"stopping": true})
			stop()
		default:
			http.NotFound(w, r)
		}
	})
}

func deliveryAtomic(path string, data []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".bridge-")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err = file.Chmod(0600); err != nil {
		file.Close()
		return err
	}
	if _, err = file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func deliveryCleanup(recordPath, qrPath, instance string) {
	// Late cleanup from a previous process must preserve its successor's QR and record.
	var current map[string]any
	data, err := os.ReadFile(recordPath)
	if err != nil || json.Unmarshal(data, &current) != nil || current["instance_id"] != instance {
		return
	}
	if err = os.Remove(qrPath); err != nil && !os.IsNotExist(err) {
		return
	}
	// The wrapper waits for this last marker to disappear before permitting restart.
	os.Remove(recordPath)
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("inquiry-whatsapp-bridge readonly-control-v1 darwin-arm64")
		return
	}
	if err := deliveryRun(); err != nil {
		fmt.Fprintln(os.Stderr, "WhatsApp connection stopped:", err)
		os.Exit(1)
	}
}

func deliveryRun() error {
	root, token, instance := os.Getenv("INQUIRY_BRIDGE_ROOT"), os.Getenv("INQUIRY_BRIDGE_TOKEN"), os.Getenv("INQUIRY_BRIDGE_INSTANCE")
	if root == "" || len(token) < 32 || instance == "" {
		return errors.New("start through the Inquiry Assistant launcher")
	}
	actual, err := os.Getwd()
	if err != nil {
		return err
	}
	if actual != filepath.Join(root, "bridge_run") {
		return errors.New("bridge working directory mismatch")
	}
	// Keep credentials and received message cache private to the logged-in OS user.
	syscall.Umask(0077)
	if err = os.MkdirAll("store", 0700); err != nil {
		return err
	}
	qrPath := filepath.Join("store", "login-qr.png")
	recordPath := "runtime.json"
	// Registered before DB/server defers: runtime removal marks completed cleanup.
	defer deliveryCleanup(recordPath, qrPath, instance)
	os.Remove(qrPath)
	logger := waLog.Stdout("Connection", "WARN", false)
	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", logger)
	if err != nil {
		return err
	}
	defer container.Close()
	device, err := container.GetFirstDevice(context.Background())
	if err != nil {
		return err
	}
	client := whatsmeow.NewClient(device, logger)
	messages, err := NewMessageStore()
	if err != nil {
		return err
	}
	defer messages.Close()
	var stateMu sync.RWMutex
	state := "starting"
	setState := func(value string) { stateMu.Lock(); state = value; stateMu.Unlock() }
	if client.Store.ID == nil {
		state = "waiting_login"
	}
	stop := make(chan struct{})
	var stopOnce sync.Once
	requestStop := func() { stopOnce.Do(func() { close(stop) }) }
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	server := &http.Server{ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 5 * time.Second}
	server.Handler = deliveryHandler(token, listener.Addr().String(), func() map[string]any {
		stateMu.RLock()
		current := state
		stateMu.RUnlock()
		return map[string]any{"install_root": root, "instance_id": instance, "state": current}
	}, requestStop)
	record, _ := json.Marshal(map[string]any{"install_root": root, "instance_id": instance, "token": token, "port": listener.Addr().(*net.TCPAddr).Port, "pid": os.Getpid()})
	if err = deliveryAtomic(recordPath, record); err != nil {
		listener.Close()
		return err
	}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil && serveErr != http.ErrServerClosed {
			requestStop()
		}
	}()
	defer server.Close()
	defer client.Disconnect()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client.AddEventHandler(func(evt interface{}) {
		switch value := evt.(type) {
		case *events.Message:
			handleMessage(client, messages, value, logger)
		case *events.HistorySync:
			handleHistorySync(client, messages, value, logger)
		case *events.Connected:
			setState("connected")
			os.Remove(qrPath)
		case *events.Disconnected:
			setState("disconnected")
		case *events.LoggedOut:
			setState("error")
			os.Remove(qrPath)
		}
	})
	// Login can wait for a phone without blocking local status or safe shutdown.
	connectDone := make(chan struct{})
	go func() {
		defer close(connectDone)
		if client.Store.ID == nil {
			channel, channelErr := client.GetQRChannel(ctx)
			if channelErr != nil {
				setState("error")
				return
			}
			if connectErr := client.ConnectContext(ctx); connectErr != nil {
				setState("error")
				return
			}
			for {
				select {
				case <-ctx.Done():
					return
				case event, ok := <-channel:
					if !ok {
						if !client.IsLoggedIn() {
							setState("error")
							os.Remove(qrPath)
						}
						return
					}
					if event.Event == "code" {
						code, encodeErr := qr.Encode(event.Code, qr.L)
						if encodeErr != nil || deliveryAtomic(qrPath, code.PNG()) != nil {
							setState("error")
							return
						}
						setState("waiting_login")
					} else if event.Event == "success" {
						setState("connected")
						os.Remove(qrPath)
						return
					} else if event.Event == "timeout" || event.Event == "error" {
						setState("error")
						os.Remove(qrPath)
						return
					}
				}
			}
		} else if connectErr := client.ConnectContext(ctx); connectErr != nil {
			setState("error")
		}
	}()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	select {
	case <-stop:
	case <-signals:
	}
	cancel()
	client.Disconnect()
	<-connectDone
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer shutdownCancel()
	return server.Shutdown(shutdownCtx)
}
