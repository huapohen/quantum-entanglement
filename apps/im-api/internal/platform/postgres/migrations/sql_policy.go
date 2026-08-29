package migrations

import "strings"

type authorityWriteFunctionSpec struct {
	argumentTokens         []string
	identityArgumentTokens []string
	resultToken            string
}

var authorityWriteFunctionSpecs = map[string]authorityWriteFunctionSpec{
	"WRITE_CONVERSATION_REVISION": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_CONVERSATION_ID", "TEXT",
			"P_EXPECTED_REVISION", "BIGINT", "P_NEXT_REVISION", "BIGINT",
			"P_WORKSPACE_ID", "TEXT", "P_CONVERSATION_TYPE", "TEXT", "P_STATUS", "TEXT",
		},
		identityArgumentTokens: []string{"TEXT", "TEXT", "BIGINT", "BIGINT", "TEXT", "TEXT", "TEXT"},
		resultToken:            "BOOLEAN",
	},
	"WRITE_PROVIDER_CONVERSATION_BINDING_REVISION": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_PROVIDER", "TEXT", "P_REALM_ID", "TEXT",
			"P_PROVIDER_CONVERSATION_ID", "TEXT", "P_EXPECTED_REVISION", "BIGINT",
			"P_NEXT_REVISION", "BIGINT", "P_CONVERSATION_ID", "TEXT", "P_STATUS", "TEXT",
		},
		identityArgumentTokens: []string{"TEXT", "TEXT", "TEXT", "TEXT", "BIGINT", "BIGINT", "TEXT", "TEXT"},
		resultToken:            "BOOLEAN",
	},
	"WRITE_CONVERSATION_MEMBERSHIP_REVISION": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_CONVERSATION_ID", "TEXT", "P_ACTOR_ID", "TEXT",
			"P_EXPECTED_REVISION", "BIGINT", "P_NEXT_REVISION", "BIGINT",
			"P_ROLE", "TEXT", "P_STATUS", "TEXT",
		},
		identityArgumentTokens: []string{"TEXT", "TEXT", "TEXT", "BIGINT", "BIGINT", "TEXT", "TEXT"},
		resultToken:            "BOOLEAN",
	},
	"WRITE_CONVERSATION_ACCESS_REVISION": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_CONVERSATION_ID", "TEXT", "P_ACTOR_ID", "TEXT",
			"P_EXPECTED_REVISION", "BIGINT", "P_NEXT_REVISION", "BIGINT",
			"P_CAN_READ", "BOOLEAN", "P_CAN_SEND_MESSAGE", "BOOLEAN",
			"P_CAN_MANAGE_MEMBERS", "BOOLEAN", "P_CAN_MANAGE_CONVERSATION", "BOOLEAN",
			"P_CAN_INVOKE_AGENT", "BOOLEAN", "P_CAN_PUBLISH_ARTIFACT_REFERENCE", "BOOLEAN",
		},
		identityArgumentTokens: []string{
			"TEXT", "TEXT", "TEXT", "BIGINT", "BIGINT",
			"BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN",
		},
		resultToken: "BOOLEAN",
	},
	"WRITE_TENANT_COMMAND_RECEIPT": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_COMMAND_KIND", "TEXT", "P_IDEMPOTENCY_KEY", "TEXT",
			"P_REQUEST_SHA256", "TEXT", "P_RESULT_SHA256", "TEXT",
		},
		identityArgumentTokens: []string{"TEXT", "TEXT", "TEXT", "TEXT", "TEXT"},
		resultToken:            "TIMESTAMPTZ",
	},
	"WRITE_EVENT": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_WORKSPACE_ID", "TEXT", "P_STREAM_ID", "TEXT",
			"P_EXPECTED_VERSION", "BIGINT", "P_EVENT_ID", "TEXT", "P_SCHEMA_VERSION", "BIGINT",
			"P_EVENT_TYPE", "TEXT", "P_ACTOR_ID", "TEXT", "P_OCCURRED_AT", "TIMESTAMPTZ",
			"P_CORRELATION_ID", "TEXT", "P_CAUSATION_ID", "TEXT", "P_IDEMPOTENCY_KEY", "TEXT",
			"P_TRACEPARENT", "TEXT", "P_PAYLOAD_KIND", "TEXT", "P_PAYLOAD_INLINE", "TEXT",
			"P_PAYLOAD_STORAGE", "TEXT", "P_PAYLOAD_REFERENCE_ID", "TEXT", "P_PAYLOAD_BYTE_LENGTH", "BIGINT",
			"P_PAYLOAD_DIGEST", "TEXT", "P_APPEND_DIGEST", "TEXT",
		},
		identityArgumentTokens: []string{
			"TEXT", "TEXT", "TEXT", "BIGINT", "TEXT", "BIGINT", "TEXT", "TEXT", "TIMESTAMPTZ",
			"TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "BIGINT", "TEXT", "TEXT",
		},
		resultToken: "BOOLEAN",
	},
	"WRITE_PROJECTION_CHECKPOINT": {
		argumentTokens: []string{
			"P_TENANT_ID", "TEXT", "P_WORKSPACE_ID", "TEXT", "P_PROJECTION_ID", "TEXT",
			"P_EXPECTED_POSITION", "BIGINT", "P_EXPECTED_CURSOR", "TEXT", "P_EXPECTED_LAST_EVENT_ID", "TEXT",
			"P_NEXT_POSITION", "BIGINT", "P_NEXT_CURSOR", "TEXT", "P_NEXT_LAST_EVENT_ID", "TEXT",
		},
		identityArgumentTokens: []string{"TEXT", "TEXT", "TEXT", "BIGINT", "TEXT", "TEXT", "BIGINT", "TEXT", "TEXT"},
		resultToken:            "BOOLEAN",
	},
}

func validMigrationStatements(sql string, allowFunctionDDL bool) bool {
	heads, ok := migrationStatementHeads(sql)
	if !ok || len(heads) == 0 {
		return false
	}
	for _, head := range heads {
		if !allowedMigrationStatement(head, allowFunctionDDL) {
			return false
		}
	}
	return true
}

func allowedMigrationStatement(head []string, allowFunctionDDL bool) bool {
	if len(head) < 2 {
		return false
	}
	for _, token := range head {
		if forbiddenMigrationToken(token) {
			return false
		}
	}
	switch head[0] {
	case "ALTER":
		return head[1] == "TABLE" && !containsMigrationToken(head, "DEFAULT")
	case "CREATE":
		if head[1] == "FUNCTION" {
			return allowFunctionDDL && validAuthorityWriteFunction(head)
		}
		if head[1] == "UNIQUE" {
			return len(head) >= 3 && head[2] == "INDEX"
		}
		if head[1] == "TABLE" {
			return !containsMigrationToken(head, "AS") && validMigrationDefaults(head)
		}
		return head[1] == "INDEX" || head[1] == "POLICY" || head[1] == "SCHEMA"
	case "DROP":
		if head[1] == "FUNCTION" {
			return allowFunctionDDL && validAuthorityWriteFunctionDrop(head)
		}
		return head[1] == "INDEX" || head[1] == "POLICY" || head[1] == "SCHEMA" ||
			head[1] == "TABLE"
	case "REVOKE":
		return allowFunctionDDL && validAuthorityWriteFunctionRevoke(head)
	default:
		return false
	}
}

func validAuthorityWriteFunction(tokens []string) bool {
	functionName, ok := authorityWriteFunctionAt(tokens, 2)
	if !ok ||
		containsMigrationToken(tokens, "OR") || containsMigrationToken(tokens, "REPLACE") ||
		containsMigrationToken(tokens, "INVOKER") || containsMigrationToken(tokens, "IMMUTABLE") ||
		containsMigrationToken(tokens, "STABLE") || containsMigrationToken(tokens, "SAFE") ||
		containsMigrationToken(tokens, "RESTRICTED") || containsMigrationToken(tokens, "CALLED") ||
		containsMigrationToken(tokens, "DEFAULT") || containsMigrationToken(tokens, "LEAKPROOF") ||
		containsMigrationToken(tokens, "QUOTED_IDENTIFIER") {
		return false
	}
	spec := authorityWriteFunctionSpecs[functionName]
	optionSuffix := []string{
		"LANGUAGE", "PLPGSQL", "VOLATILE", "STRICT", "SECURITY", "DEFINER",
		"PARALLEL", "UNSAFE", "SET", "SEARCH_PATH", "TO", "PG_CATALOG", "AS", "LITERAL",
	}
	optionOffset := len(tokens) - len(optionSuffix)
	if optionOffset < 6 || tokens[optionOffset-2] != "RETURNS" ||
		tokens[optionOffset-1] != spec.resultToken ||
		!equalMigrationTokens(tokens[4:optionOffset-2], spec.argumentTokens) ||
		!containsMigrationSequence(tokens[optionOffset:], optionSuffix...) {
		return false
	}
	for _, token := range append([]string{"RETURNS"}, optionSuffix...) {
		if migrationTokenCount(tokens, token) != 1 {
			return false
		}
	}
	return migrationTokenCount(tokens, "WANWORK_IM") == 1 &&
		migrationAuthorityWriteFunctionCount(tokens) == 1
}

func validAuthorityWriteFunctionDrop(tokens []string) bool {
	functionName, ok := authorityWriteFunctionAt(tokens, 2)
	return ok && equalMigrationTokens(
		tokens[4:],
		authorityWriteFunctionSpecs[functionName].identityArgumentTokens,
	) && migrationTokenCount(tokens, "WANWORK_IM") == 1 &&
		migrationAuthorityWriteFunctionCount(tokens) == 1 &&
		!containsMigrationToken(tokens, "CASCADE") && !containsMigrationToken(tokens, "IF") &&
		!containsMigrationToken(tokens, "RESTRICT")
}

func validAuthorityWriteFunctionRevoke(tokens []string) bool {
	functionName, ok := authorityWriteFunctionAt(tokens, 4)
	return ok && len(tokens) >= 8 && tokens[1] == "ALL" && tokens[2] == "ON" &&
		tokens[3] == "FUNCTION" && tokens[len(tokens)-2] == "FROM" &&
		tokens[len(tokens)-1] == "PUBLIC" && migrationTokenCount(tokens, "FROM") == 1 &&
		migrationTokenCount(tokens, "PUBLIC") == 1 &&
		migrationTokenCount(tokens, "WANWORK_IM") == 1 &&
		migrationAuthorityWriteFunctionCount(tokens) == 1 &&
		equalMigrationTokens(
			tokens[6:len(tokens)-2],
			authorityWriteFunctionSpecs[functionName].identityArgumentTokens,
		) &&
		!containsMigrationToken(tokens, "GRANT")
}

func authorityWriteFunctionAt(tokens []string, offset int) (string, bool) {
	if len(tokens) <= offset+1 || tokens[offset] != "WANWORK_IM" ||
		!authorityWriteFunctionName(tokens[offset+1]) {
		return "", false
	}
	return tokens[offset+1], true
}

func authorityWriteFunctionName(value string) bool {
	_, exists := authorityWriteFunctionSpecs[value]
	return exists
}

func migrationAuthorityWriteFunctionCount(tokens []string) int {
	count := 0
	for _, token := range tokens {
		if authorityWriteFunctionName(token) {
			count++
		}
	}
	return count
}

func equalMigrationTokens(left []string, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func migrationTokenCount(tokens []string, expected string) int {
	count := 0
	for _, token := range tokens {
		if token == expected {
			count++
		}
	}
	return count
}

func containsMigrationSequence(tokens []string, expected ...string) bool {
	if len(expected) == 0 || len(expected) > len(tokens) {
		return false
	}
	for offset := 0; offset <= len(tokens)-len(expected); offset++ {
		matched := true
		for index, token := range expected {
			if tokens[offset+index] != token {
				matched = false
				break
			}
		}
		if matched {
			return true
		}
	}
	return false
}

func forbiddenMigrationToken(token string) bool {
	switch token {
	case "CALL", "COPY", "DBLINK", "EXECUTE", "LO_EXPORT", "LO_IMPORT",
		"PG_ADVISORY_LOCK", "PG_CANCEL_BACKEND", "PG_CREATE_RESTORE_POINT",
		"PG_LOGICAL_EMIT_MESSAGE", "PG_READ_BINARY_FILE", "PG_READ_FILE",
		"PG_RELOAD_CONF", "PG_ROTATE_LOGFILE", "PG_SLEEP", "PG_SWITCH_WAL",
		"PG_TERMINATE_BACKEND", "PG_WRITE_FILE", "SELECT", "SET_CONFIG":
		return true
	default:
		return false
	}
}

func containsMigrationToken(tokens []string, expected string) bool {
	for _, token := range tokens {
		if token == expected {
			return true
		}
	}
	return false
}

func validMigrationDefaults(tokens []string) bool {
	for index, token := range tokens {
		if token == "DEFAULT" && (index+1 >= len(tokens) ||
			(tokens[index+1] != "CLOCK_TIMESTAMP" && tokens[index+1] != "LITERAL")) {
			return false
		}
	}
	return true
}

func migrationStatementHeads(sql string) ([][]string, bool) {
	heads := make([][]string, 0)
	current := make([]string, 0, 3)
	for offset := 0; offset < len(sql); {
		switch {
		case sql[offset] == ';':
			if len(current) != 0 {
				heads = append(heads, current)
				current = make([]string, 0, 3)
			}
			offset++
		case isSQLSpace(sql[offset]):
			offset++
		case offset+1 < len(sql) && sql[offset] == '-' && sql[offset+1] == '-':
			offset += 2
			for offset < len(sql) && sql[offset] != '\n' {
				offset++
			}
		case offset+1 < len(sql) && sql[offset] == '/' && sql[offset+1] == '*':
			var ok bool
			offset, ok = skipSQLBlockComment(sql, offset)
			if !ok {
				return nil, false
			}
		case sql[offset] == '\'':
			var ok bool
			offset, ok = skipSQLQuoted(sql, offset, '\'')
			if !ok {
				return nil, false
			}
			current = append(current, "LITERAL")
		case sql[offset] == '"':
			var ok bool
			offset, ok = skipSQLQuoted(sql, offset, '"')
			if !ok {
				return nil, false
			}
			current = append(current, "QUOTED_IDENTIFIER")
		case sql[offset] == '$':
			next, matched, ok := skipSQLDollarQuote(sql, offset)
			if !ok {
				return nil, false
			}
			if matched {
				offset = next
				current = append(current, "LITERAL")
			} else {
				offset++
			}
		case isSQLIdentifierStart(sql[offset]):
			start := offset
			offset++
			for offset < len(sql) && isSQLIdentifierPart(sql[offset]) {
				offset++
			}
			current = append(current, strings.ToUpper(sql[start:offset]))
		default:
			offset++
		}
	}
	if len(current) != 0 {
		heads = append(heads, current)
	}
	return heads, true
}

func skipSQLBlockComment(sql string, offset int) (int, bool) {
	depth := 1
	offset += 2
	for offset < len(sql) {
		switch {
		case offset+1 < len(sql) && sql[offset] == '/' && sql[offset+1] == '*':
			depth++
			offset += 2
		case offset+1 < len(sql) && sql[offset] == '*' && sql[offset+1] == '/':
			depth--
			offset += 2
			if depth == 0 {
				return offset, true
			}
		default:
			offset++
		}
	}
	return offset, false
}

func skipSQLQuoted(sql string, offset int, delimiter byte) (int, bool) {
	offset++
	for offset < len(sql) {
		if sql[offset] != delimiter {
			offset++
			continue
		}
		if offset+1 < len(sql) && sql[offset+1] == delimiter {
			offset += 2
			continue
		}
		return offset + 1, true
	}
	return offset, false
}

func skipSQLDollarQuote(sql string, offset int) (int, bool, bool) {
	tagEnd := offset + 1
	for tagEnd < len(sql) && isSQLDollarTagPart(sql[tagEnd]) {
		tagEnd++
	}
	if tagEnd >= len(sql) || sql[tagEnd] != '$' {
		return offset, false, true
	}
	tag := sql[offset : tagEnd+1]
	closingOffset := strings.Index(sql[tagEnd+1:], tag)
	if closingOffset < 0 {
		return len(sql), true, false
	}
	return tagEnd + 1 + closingOffset + len(tag), true, true
}

func isSQLSpace(value byte) bool {
	return value == ' ' || value == '\t' || value == '\n' || value == '\r' || value == '\f'
}

func isSQLIdentifierStart(value byte) bool {
	return value == '_' || value >= 'A' && value <= 'Z' || value >= 'a' && value <= 'z'
}

func isSQLIdentifierPart(value byte) bool {
	return isSQLIdentifierStart(value) || value >= '0' && value <= '9' || value == '$'
}

func isSQLDollarTagPart(value byte) bool {
	return value == '_' || value >= 'A' && value <= 'Z' || value >= 'a' && value <= 'z' ||
		value >= '0' && value <= '9'
}
