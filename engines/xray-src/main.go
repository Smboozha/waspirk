// sarpik-xray — запуск вендоренного форка Xray-core (новые транспорты
// VLESS/REALITY/XHTTP для Sarpik) с TUN-интерфейсом на Linux.
//
// Конфигурация подаётся файлом JSON в стандартном формате Xray. Свой pid
// пишется в файл, чтобы приложение могло остановить ядро через sudo kill.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"

	core "github.com/xtls/xray-core/core"
	"github.com/xtls/xray-core/infra/conf/serial"

	_ "sarpik-xray/registry"
)

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "sarpik-xray:", err)
	os.Exit(1)
}

func main() {
	configPath := flag.String("config", "", "путь к конфигурации Xray (JSON)")
	pidfile := flag.String("pidfile", "", "файл для записи pid процесса")
	flag.Parse()

	if *configPath == "" {
		fmt.Fprintln(os.Stderr, "использование: sarpik-xray -config <файл> [-pidfile <файл>]")
		os.Exit(2)
	}

	if *pidfile != "" {
		if err := os.WriteFile(*pidfile, []byte(fmt.Sprintf("%d\n", os.Getpid())), 0o644); err != nil {
			fatal(fmt.Errorf("pidfile: %w", err))
		}
		defer os.Remove(*pidfile)
	}

	raw, err := os.ReadFile(*configPath)
	if err != nil {
		fatal(err)
	}
	config, err := serial.LoadJSONConfig(strings.NewReader(string(raw)))
	if err != nil {
		fatal(fmt.Errorf("config: %w", err))
	}
	instance, err := core.New(config)
	if err != nil {
		fatal(fmt.Errorf("new: %w", err))
	}
	if err := instance.Start(); err != nil {
		fatal(fmt.Errorf("start: %w", err))
	}
	fmt.Println("sarpik-xray:", core.Version(), "running")

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	<-signals

	instance.Close()
	fmt.Println("sarpik-xray: stopped")
}