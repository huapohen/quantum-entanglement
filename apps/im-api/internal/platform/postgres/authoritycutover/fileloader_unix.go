//go:build darwin || linux

package authoritycutover

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"golang.org/x/sys/unix"
	"golang.org/x/text/unicode/norm"
)

const maximumInputPathBytes = 4096

type trustedFileFingerprint struct {
	device          uint64
	inode           uint64
	mode            uint32
	linkCount       uint64
	ownerUID        uint32
	ownerGID        uint32
	size            int64
	modifiedSeconds int64
	modifiedNanos   int64
	changedSeconds  int64
	changedNanos    int64
}

func readTrustedRegularFile(
	path string,
	identity TrustedFileIdentity,
	maximumBytes int,
) ([]byte, error) {
	if maximumBytes <= 0 || maximumBytes > maximumPlanBytes || !validInputPath(path) {
		return nil, ErrUnsafeInputFile
	}
	descriptor, err := openNoSymlinkRegularFile(path)
	if err != nil {
		return nil, ErrUnsafeInputFile
	}
	file := os.NewFile(uintptr(descriptor), "authority-cutover-input")
	if file == nil {
		_ = unix.Close(descriptor)
		return nil, ErrUnsafeInputFile
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()

	before, err := trustedFileFingerprintForDescriptor(descriptor)
	if err != nil || !validTrustedFileFingerprint(before, identity, maximumBytes) {
		return nil, ErrUnsafeInputFile
	}
	raw, err := io.ReadAll(io.LimitReader(file, int64(maximumBytes)+1))
	if err != nil || len(raw) == 0 || len(raw) > maximumBytes || int64(len(raw)) != before.size {
		return nil, ErrUnsafeInputFile
	}
	after, err := trustedFileFingerprintForDescriptor(descriptor)
	if err != nil || after != before {
		return nil, ErrInputFileChanged
	}
	if err := file.Close(); err != nil {
		return nil, ErrInputFileChanged
	}
	closed = true
	return raw, nil
}

func validInputPath(path string) bool {
	return path != "" && len(path) <= maximumInputPathBytes && filepath.IsAbs(path) &&
		filepath.Clean(path) == path && path != string(filepath.Separator) &&
		!strings.ContainsRune(path, '\x00') && utf8.ValidString(path) && norm.NFC.IsNormalString(path)
}

func openNoSymlinkRegularFile(path string) (int, error) {
	components := strings.Split(strings.TrimPrefix(path, string(filepath.Separator)), string(filepath.Separator))
	if len(components) == 0 {
		return -1, ErrUnsafeInputFile
	}
	directory, err := unix.Open(
		string(filepath.Separator),
		unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW,
		0,
	)
	if err != nil {
		return -1, err
	}
	for _, component := range components[:len(components)-1] {
		next, openErr := unix.Openat(
			directory,
			component,
			unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW,
			0,
		)
		closeErr := unix.Close(directory)
		if openErr != nil {
			return -1, openErr
		}
		if closeErr != nil {
			_ = unix.Close(next)
			return -1, closeErr
		}
		directory = next
	}
	descriptor, openErr := unix.Openat(
		directory,
		components[len(components)-1],
		unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK,
		0,
	)
	closeErr := unix.Close(directory)
	if openErr != nil {
		return -1, openErr
	}
	if closeErr != nil {
		_ = unix.Close(descriptor)
		return -1, closeErr
	}
	return descriptor, nil
}

func validTrustedFileFingerprint(
	fingerprint trustedFileFingerprint,
	identity TrustedFileIdentity,
	maximumBytes int,
) bool {
	return fingerprint.mode&unix.S_IFMT == unix.S_IFREG && fingerprint.linkCount == 1 &&
		fingerprint.ownerUID == identity.OwnerUID && fingerprint.ownerGID == identity.OwnerGID &&
		fingerprint.mode&0o7777 == uint32(identity.Mode.Perm()) && fingerprint.size > 0 &&
		fingerprint.size <= int64(maximumBytes)
}
