//go:build darwin || linux

package authoritycutover

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"golang.org/x/sys/unix"
)

func TestLoadPlanFileUsesExactDescriptorIdentityAndReturnsDetachedPlan(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	path := writeTrustedTestFile(t, plan.CanonicalBytes(), 0o400)
	identity := currentTestFileIdentity(0o400)
	loaded, err := LoadPlanFile(path, identity)
	if err != nil {
		t.Fatalf("LoadPlanFile: %v", err)
	}
	if loaded.Digest() != plan.Digest() || !slices.Equal(loaded.CanonicalBytes(), plan.CanonicalBytes()) {
		t.Fatal("loaded plan differs from canonical source")
	}
	copy := loaded.CanonicalBytes()
	copy[0] ^= 0xff
	if !slices.Equal(loaded.CanonicalBytes(), plan.CanonicalBytes()) {
		t.Fatal("caller mutation changed loaded plan")
	}

	groupReadable := writeTrustedTestFile(t, plan.CanonicalBytes(), 0o440)
	if _, err := LoadPlanFile(groupReadable, currentTestFileIdentity(0o440)); err != nil {
		t.Fatalf("LoadPlanFile group-readable policy: %v", err)
	}
}

func TestLoadPlanFileRejectsUnsafePathAndInodeShapes(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	identity := currentTestFileIdentity(0o400)
	regular := writeTrustedTestFile(t, plan.CanonicalBytes(), 0o400)
	directory := filepath.Dir(regular)

	finalSymlink := filepath.Join(directory, "plan-link.json")
	if err := os.Symlink(regular, finalSymlink); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	parentTarget := t.TempDir()
	parentFile := filepath.Join(parentTarget, "plan.json")
	if err := os.WriteFile(parentFile, plan.CanonicalBytes(), 0o400); err != nil {
		t.Fatalf("write parent target: %v", err)
	}
	parentSymlink := filepath.Join(t.TempDir(), "linked-parent")
	if err := os.Symlink(parentTarget, parentSymlink); err != nil {
		t.Fatalf("parent symlink: %v", err)
	}
	hardlink := filepath.Join(directory, "plan-hardlink.json")
	if err := os.Link(regular, hardlink); err != nil {
		t.Fatalf("hardlink: %v", err)
	}
	fifo := filepath.Join(t.TempDir(), "plan.fifo")
	if err := unix.Mkfifo(fifo, 0o400); err != nil {
		t.Fatalf("mkfifo: %v", err)
	}

	paths := map[string]string{
		"relative":       filepath.Base(regular),
		"unclean":        filepath.Join(directory, "..", filepath.Base(directory), filepath.Base(regular)),
		"final symlink":  finalSymlink,
		"parent symlink": filepath.Join(parentSymlink, "plan.json"),
		"hardlink":       regular,
		"directory":      directory,
		"fifo":           fifo,
	}
	for name, path := range paths {
		t.Run(name, func(t *testing.T) {
			if _, err := LoadPlanFile(path, identity); !errors.Is(err, ErrUnsafeInputFile) {
				t.Fatalf("LoadPlanFile error = %v, want %v", err, ErrUnsafeInputFile)
			}
		})
	}
}

func TestLoadPlanFileRejectsOwnershipModeSizeAndContentDrift(t *testing.T) {
	plan, err := BuildPlan(validPlanInput())
	if err != nil {
		t.Fatalf("BuildPlan: %v", err)
	}
	validPath := writeTrustedTestFile(t, plan.CanonicalBytes(), 0o400)
	validIdentity := currentTestFileIdentity(0o400)
	tests := map[string]struct {
		path     string
		identity TrustedFileIdentity
		want     error
	}{
		"invalid policy": {
			path: validPath, identity: currentTestFileIdentity(0o600), want: ErrInvalidFileIdentity,
		},
		"wrong owner": {
			path: validPath,
			identity: TrustedFileIdentity{
				OwnerUID: validIdentity.OwnerUID + 1,
				OwnerGID: validIdentity.OwnerGID,
				Mode:     0o400,
			},
			want: ErrUnsafeInputFile,
		},
		"wrong group": {
			path: validPath,
			identity: TrustedFileIdentity{
				OwnerUID: validIdentity.OwnerUID,
				OwnerGID: validIdentity.OwnerGID + 1,
				Mode:     0o400,
			},
			want: ErrUnsafeInputFile,
		},
		"wrong file mode": {
			path: writeTrustedTestFile(t, plan.CanonicalBytes(), 0o600), identity: validIdentity, want: ErrUnsafeInputFile,
		},
		"empty": {
			path: writeTrustedTestFile(t, nil, 0o400), identity: validIdentity, want: ErrUnsafeInputFile,
		},
		"oversized": {
			path: writeTrustedTestFile(t, make([]byte, maximumPlanBytes+1), 0o400), identity: validIdentity, want: ErrUnsafeInputFile,
		},
		"invalid plan": {
			path: writeTrustedTestFile(t, []byte(`{"format":"invalid"}`), 0o400), identity: validIdentity, want: ErrInvalidPlan,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := LoadPlanFile(test.path, test.identity); !errors.Is(err, test.want) {
				t.Fatalf("LoadPlanFile error = %v, want %v", err, test.want)
			}
		})
	}
}

func writeTrustedTestFile(t *testing.T, raw []byte, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "plan.json")
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatalf("write test file: %v", err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatalf("chmod test file: %v", err)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatalf("resolve test path: %v", err)
	}
	return resolved
}

func currentTestFileIdentity(mode os.FileMode) TrustedFileIdentity {
	return TrustedFileIdentity{
		OwnerUID: uint32(os.Getuid()),
		OwnerGID: uint32(os.Getgid()),
		Mode:     mode,
	}
}
