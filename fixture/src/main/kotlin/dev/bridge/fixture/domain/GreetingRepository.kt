package dev.bridge.fixture.domain

import org.springframework.data.jpa.repository.JpaRepository

interface GreetingRepository : JpaRepository<Greeting, Long> {
    fun findByRecipient(recipient: String): List<Greeting>
    fun findByRecipientIgnoreCase(recipient: String): List<Greeting>
}
