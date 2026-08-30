//go:build darwin || linux

package authoritycutover

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVerifyApprovalFileReturnsOnlyVerifiedEvidence(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	path := writeTrustedTestFile(t, fixture.raw, 0o400)
	verified, err := VerifyApprovalFile(
		fixture.plan,
		path,
		currentTestFileIdentity(0o400),
		fixture.verifier,
		fixture.now,
	)
	if err != nil {
		t.Fatalf("VerifyApprovalFile: %v", err)
	}
	if verified.ApprovalDigest() != approvalEvidenceDigest(fixture.raw) ||
		verified.KeyFingerprint() != approvalKeyFingerprint(fixture.publicKey) ||
		verified.PlanDigest() != fixture.plan.Digest() {
		t.Fatalf("verified file evidence is incomplete: %+v", verified)
	}

	groupReadable := writeTrustedTestFile(t, fixture.raw, 0o440)
	if _, err := VerifyApprovalFile(
		fixture.plan,
		groupReadable,
		currentTestFileIdentity(0o440),
		fixture.verifier,
		fixture.now,
	); err != nil {
		t.Fatalf("VerifyApprovalFile group-readable policy: %v", err)
	}
}

func TestVerifyApprovalFileRejectsUnsafeInodesBeforeVerification(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	identity := currentTestFileIdentity(0o400)
	regular := writeTrustedTestFile(t, fixture.raw, 0o400)
	directory := filepath.Dir(regular)

	symlink := filepath.Join(directory, "approval-link.json")
	if err := os.Symlink(regular, symlink); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	hardlink := filepath.Join(directory, "approval-hardlink.json")
	if err := os.Link(regular, hardlink); err != nil {
		t.Fatalf("hardlink: %v", err)
	}
	tests := map[string]string{
		"relative":      filepath.Base(regular),
		"final symlink": symlink,
		"hardlink":      regular,
		"directory":     directory,
	}
	for name, path := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := VerifyApprovalFile(
				fixture.plan,
				path,
				identity,
				fixture.verifier,
				fixture.now,
			); !errors.Is(err, ErrUnsafeInputFile) {
				t.Fatalf("VerifyApprovalFile error = %v, want %v", err, ErrUnsafeInputFile)
			}
		})
	}
}

func TestVerifyApprovalFileRejectsIdentitySizeContentAndTimeFailures(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	validPath := writeTrustedTestFile(t, fixture.raw, 0o400)
	validIdentity := currentTestFileIdentity(0o400)
	tests := map[string]struct {
		path     string
		identity TrustedFileIdentity
		want     error
	}{
		"invalid identity policy": {
			path: validPath, identity: currentTestFileIdentity(0o600), want: ErrInvalidFileIdentity,
		},
		"wrong owner": {
			path: validPath,
			identity: TrustedFileIdentity{
				OwnerUID: validIdentity.OwnerUID + 1,
				OwnerGID: validIdentity.OwnerGID,
				Mode:     validIdentity.Mode,
			},
			want: ErrUnsafeInputFile,
		},
		"wrong mode": {
			path: writeTrustedTestFile(t, fixture.raw, 0o600), identity: validIdentity, want: ErrUnsafeInputFile,
		},
		"empty": {
			path: writeTrustedTestFile(t, nil, 0o400), identity: validIdentity, want: ErrUnsafeInputFile,
		},
		"oversized": {
			path:     writeTrustedTestFile(t, bytes.Repeat([]byte{'x'}, maximumApprovalBytes+1), 0o400),
			identity: validIdentity,
			want:     ErrUnsafeInputFile,
		},
		"invalid approval": {
			path:     writeTrustedTestFile(t, []byte(`{"format":"invalid"}`), 0o400),
			identity: validIdentity,
			want:     ErrInvalidApproval,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := VerifyApprovalFile(
				fixture.plan,
				test.path,
				test.identity,
				fixture.verifier,
				fixture.now,
			); !errors.Is(err, test.want) {
				t.Fatalf("VerifyApprovalFile error = %v, want %v", err, test.want)
			}
		})
	}
	if _, err := VerifyApprovalFile(
		fixture.plan,
		validPath,
		validIdentity,
		fixture.verifier,
		fixture.expiresAt,
	); !errors.Is(err, ErrExpiredApproval) {
		t.Fatalf("expired file approval error = %v, want %v", err, ErrExpiredApproval)
	}
}

func TestVerifyApprovalFileErrorsDoNotDisclosePathOrRawBytes(t *testing.T) {
	fixture := newApprovalFixture(t, 0)
	pathCanary := "approval-path-canary"
	directory := filepath.Join(t.TempDir(), pathCanary)
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	rawCanary := []byte(`{"signature":"approval-file-signature-canary"}`)
	path := filepath.Join(directory, "approval.json")
	if err := os.WriteFile(path, rawCanary, 0o400); err != nil {
		t.Fatalf("write approval: %v", err)
	}
	if err := os.Chmod(path, 0o400); err != nil {
		t.Fatalf("chmod approval: %v", err)
	}
	_, verifyErr := VerifyApprovalFile(
		fixture.plan,
		path,
		currentTestFileIdentity(0o400),
		fixture.verifier,
		fixture.now,
	)
	for _, rendered := range []string{fmt.Sprint(verifyErr), fmt.Sprintf("%+v", verifyErr)} {
		if strings.Contains(rendered, pathCanary) || strings.Contains(rendered, string(rawCanary)) ||
			strings.Contains(rendered, "approval-file-signature-canary") {
			t.Fatalf("approval file error disclosed path or content: %q", rendered)
		}
	}
}
